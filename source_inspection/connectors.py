from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from config import SEARCH_TERMS, settings
from scoring import classify_salary_type

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _money(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_description(requirement: Any, responsibility: Any, full: Any = None) -> str:
    vals: list[str] = []
    def flatten(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for nested in value.values():
                flatten(nested)
        elif isinstance(value, list):
            for nested in value:
                flatten(nested)
        else:
            vals.append(re.sub(r"<[^>]+>", " ", str(value)))
    for value in (requirement, responsibility, full):
        flatten(value)
    return " ".join(x for x in vals if x)[:12000]


def _hh_area_id(data: list[dict[str, Any]], wanted: str = "сочи") -> str | None:
    found: list[str] = []
    def walk(node: Any, parent_names: list[str]) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, parent_names)
        elif isinstance(node, dict):
            name = str(node.get("name", "")).lower().replace("ё", "е")
            names = parent_names + [name]
            if name == wanted and any("краснодар" in x for x in parent_names):
                found.append(str(node.get("id")))
            for key in ("areas", "items"):
                if key in node:
                    walk(node[key], names)
    walk(data, [])
    return found[0] if found else None


class VacancyConnectors:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
        self._hh_area: str | None = None
        self._sj_town: int | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def _get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def hh(self) -> tuple[list[dict[str, Any]], str]:
        headers = {"User-Agent": settings.hh_user_agent}
        if not self._hh_area:
            areas = await self._get_json("https://api.hh.ru/areas", headers=headers)
            self._hh_area = _hh_area_id(areas)
            if not self._hh_area:
                raise RuntimeError("В справочнике HeadHunter не найден город Сочи в Краснодарском крае")
        gathered: list[dict[str, Any]] = []
        for term in SEARCH_TERMS:
            data = await self._get_json(
                "https://api.hh.ru/vacancies",
                params={"text": term, "area": self._hh_area, "per_page": 30, "page": 0, "order_by": "publication_time"},
                headers=headers,
            )
            for item in data.get("items", []):
                salary = item.get("salary") or {}
                salary_type = "unknown" if salary.get("gross") is None else ("gross" if salary.get("gross") else "net")
                desc = _compact_description(item.get("snippet", {}).get("requirement"), item.get("snippet", {}).get("responsibility"))
                gathered.append({
                    "source": "HeadHunter", "external_id": str(item.get("id")),
                    "title": item.get("name", ""), "employer": (item.get("employer") or {}).get("name", "Работодатель не указан"),
                    "city": (item.get("area") or {}).get("name", ""), "description": desc,
                    "salary_min": _money(salary.get("from")), "salary_max": _money(salary.get("to")),
                    "currency": salary.get("currency"), "salary_type": salary_type,
                    "schedule": (item.get("schedule") or {}).get("name", ""),
                    "experience": (item.get("experience") or {}).get("name", ""),
                    "published_at": item.get("published_at"), "url": item.get("alternate_url", ""),
                })
            await asyncio.sleep(0.15)
        return gathered, "API HeadHunter: публичный поиск; OAuth работодателя/соискателя для действий от имени аккаунта настраивается отдельно"

    async def superjob(self) -> tuple[list[dict[str, Any]], str]:
        if not settings.superjob_app_id:
            return [], "Не подключён: нужен ключ приложения SUPERJOB_APP_ID из кабинета разработчика SuperJob"
        headers = {"X-Api-App-Id": settings.superjob_app_id}
        if self._sj_town is None:
            towns = await self._get_json("https://api.superjob.ru/2.0/towns/", params={"keyword": settings.target_city, "count": 10}, headers=headers)
            objects = towns.get("objects", [])
            if objects:
                self._sj_town = int(objects[0].get("id"))
        if self._sj_town is None:
            return [], "Не удалось определить идентификатор Сочи в справочнике SuperJob"
        gathered: list[dict[str, Any]] = []
        for term in SEARCH_TERMS:
            data = await self._get_json(
                "https://api.superjob.ru/2.0/vacancies/",
                params={"keyword": term, "town": self._sj_town, "count": 30, "page": 0, "payment_from": 1},
                headers=headers,
            )
            for item in data.get("objects", []):
                description = _compact_description(item.get("candidat"), item.get("work"), item.get("vacancyRichText"))
                explicit = classify_salary_type(description)
                gathered.append({
                    "source": "SuperJob", "external_id": str(item.get("id")),
                    "title": item.get("profession", ""), "employer": (item.get("firm_name") or "Работодатель не указан"),
                    "city": (item.get("town") or {}).get("title", settings.target_city), "description": description,
                    "salary_min": _money(item.get("payment_from")), "salary_max": _money(item.get("payment_to")),
                    "currency": item.get("currency", "rub"), "salary_type": explicit,
                    "schedule": (item.get("type_of_work") or {}).get("title", ""),
                    "experience": (item.get("experience") or {}).get("title", ""),
                    "published_at": None, "url": item.get("link", ""),
                })
            await asyncio.sleep(0.15)
        return gathered, "Поиск вакансий через официальный API SuperJob; для откликов нужна авторизация и ID резюме"

    async def trudvsem(self) -> tuple[list[dict[str, Any]], str]:
        gathered: list[dict[str, Any]] = []
        base = "https://opendata.trudvsem.ru/api/v1/vacancies/region/" + quote_plus(settings.target_region_code)
        for term in SEARCH_TERMS:
            data = await self._get_json(base, params={"text": term, "limit": 100, "offset": 0})
            results = data.get("results", {})
            items = results.get("vacancies", []) if isinstance(results, dict) else []
            for wrapper in items:
                item = wrapper.get("vacancy", wrapper) if isinstance(wrapper, dict) else {}
                address_obj = item.get("addresses") or {}
                address_items = address_obj.get("address", []) if isinstance(address_obj, dict) else []
                address_locations = []
                if isinstance(address_items, list):
                    for addr in address_items:
                        if isinstance(addr, dict) and addr.get("location"):
                            address_locations.append(str(addr.get("location")))
                region_obj = item.get("region") or ""
                region_name = region_obj.get("name", "") if isinstance(region_obj, dict) else str(region_obj)
                city_raw = item.get("town") or item.get("city") or ""
                if isinstance(city_raw, dict):
                    city_raw = city_raw.get("name") or city_raw.get("title") or city_raw.get("text") or ""
                city_text = " ".join([str(safe) for safe in [city_raw, *address_locations, region_name] if safe])
                description = _compact_description(item.get("requirement"), item.get("duty"), item.get("job-name") or item.get("profession"))
                salary_raw = item.get("salary")
                if isinstance(salary_raw, dict):
                    s_min = _money(salary_raw.get("from") or salary_raw.get("min"))
                    s_max = _money(salary_raw.get("to") or salary_raw.get("max"))
                else:
                    s_min = _money(item.get("salary_min") or salary_raw)
                    s_max = _money(item.get("salary_max"))
                city = item.get("town") or item.get("city") or city_text
                gathered.append({
                    "source": "Работа России", "external_id": str(item.get("id") or item.get("vacancy_id") or item.get("source_id") or ""),
                    "title": item.get("job-name") or item.get("profession") or item.get("vacancy_name") or item.get("position") or "",
                    "employer": item.get("company", {}).get("name", "Работодатель не указан") if isinstance(item.get("company"), dict) else (item.get("company") or "Работодатель не указан"),
                    "city": city_text, "description": description,
                    "salary_min": s_min, "salary_max": s_max, "currency": "RUB",
                    "salary_type": classify_salary_type(description + " " + str(item.get("salary_comment", ""))),
                    "schedule": item.get("schedule") or item.get("schedule_name") or "",
                    "experience": item.get("experience") or item.get("experience_name") or "",
                    "published_at": item.get("date_pub") or item.get("date_update"),
                    "url": item.get("vac_url") or item.get("url") or "https://trudvsem.ru/",
                })
            await asyncio.sleep(0.15)
        # Public API is regional; retain only records explicitly identifying Sochi in city/address fields.
        gathered = [x for x in gathered if "сочи" in str(x.get("city", "")).lower()]
        return gathered, "Открытый региональный API Роструда; выдача Краснодарского края дополнительно отфильтрована по Сочи"

    async def run_all(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        all_items: list[dict[str, Any]] = []
        statuses: dict[str, str] = {}
        for name, func in (("HeadHunter", self.hh), ("SuperJob", self.superjob), ("Работа России", self.trudvsem)):
            try:
                items, note = await func()
                all_items.extend(items)
                statuses[name] = f"Получено {len(items)} вакансий. {note}"
            except httpx.HTTPStatusError as exc:
                statuses[name] = f"Ошибка HTTP {exc.response.status_code}: {exc.response.text[:250]}"
            except Exception as exc:
                statuses[name] = f"Ошибка подключения/обработки: {type(exc).__name__}: {str(exc)[:250]}"
        statuses["Зарплата.ру"] = "Ручной источник: официальная программная интеграция пока не настроена; используйте поиск по ссылке"
        statuses["Работа.ру"] = "Ручной источник: официальный доступ к API для данного приложения не подтверждён"
        statuses["Авито Работа"] = "Ручной источник: не используется скрытый сбор данных"
        statuses["Сайты работодателей"] = "Ручной источник: добавляйте проверенные прямые ссылки на страницы вакансий"
        return all_items, statuses
