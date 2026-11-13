from __future__ import annotations

import asyncio
import re
from html import unescape as html_unescape
import xml.etree.ElementTree as ET
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



def _rss_plain_text(value: Any) -> str:
    """Turn RSS HTML fragments into readable text while preserving field boundaries."""
    text = html_unescape(str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def _rss_fields(node: ET.Element) -> dict[str, str]:
    fields: dict[str, str] = {}
    for child in node:
        key = child.tag.rsplit("}", 1)[-1].lower()
        if key == "link":
            value = child.attrib.get("href") or "".join(child.itertext())
        else:
            value = "".join(child.itertext())
        fields[key] = _rss_plain_text(value)
    return fields


def _rss_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?im)(?:^|\\n)\\s*(?:{names})\\s*[:—-]\\s*([^\\n]+)", text)
    return match.group(1).strip(" \\t.;") if match else ""


def _rss_salary(text: str, separate_salary: str = "") -> tuple[float | None, float | None, str | None, str]:
    # Parse only lines that look like salary information; do not mistake IDs or other numbers for pay.
    lines = [line for line in (separate_salary + "\n" + text).splitlines()
             if any(token in line.lower() for token in ("зарплат", "оплат", "доход", "₽", "руб"))]
    candidate = " ".join(lines)
    pattern = (
        r"(?i)(?:от\\s*)?(\\d[\\d\\s\\u00a0\\u202f]{2,})"
        r"(?:\\s*(?:[-–—]|до)\\s*(\\d[\\d\\s\\u00a0\\u202f]{2,}))?"
        r"\\s*(?:₽|руб(?:\\.|лей|ля|ль)?)"
    )
    match = re.search(pattern, candidate)
    if not match:
        return None, None, None, classify_salary_type(candidate)
    def to_number(value: str | None) -> float | None:
        if not value:
            return None
        digits = re.sub(r"\\D", "", value)
        return float(digits) if digits else None
    low, high = to_number(match.group(1)), to_number(match.group(2))
    return low, high, "RUB", classify_salary_type(candidate)


def _parse_hh_rss(xml_content: bytes, area_name: str = "Сочи") -> list[dict[str, Any]]:
    """Parse HeadHunter's public RSS search feed into the agent's common vacancy shape."""
    root = ET.fromstring(xml_content)
    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in nodes:
        fields = _rss_fields(node)
        title = fields.get("title", "").strip()
        url = fields.get("link") or fields.get("guid") or ""
        description = fields.get("description") or fields.get("summary") or fields.get("content") or ""
        if not title or not url:
            continue
        id_match = re.search(r"/vacancy/(\\d+)", url)
        external_id = id_match.group(1) if id_match else (fields.get("guid") or url)
        if external_id in seen:
            continue
        seen.add(external_id)
        extra_salary = fields.get("salary", "")
        salary_min, salary_max, currency, salary_type = _rss_salary(description, extra_salary)
        full_description = " ".join(part for part in (description, extra_salary) if part).strip()[:12000]
        employer = (
            fields.get("company") or fields.get("employer")
            or _rss_labeled_value(description, ("Компания", "Работодатель", "Организация"))
            or "Работодатель не указан"
        )
        schedule = (
            fields.get("schedule")
            or _rss_labeled_value(description, ("График работы", "График", "Режим работы"))
        )
        experience = (
            fields.get("experience")
            or _rss_labeled_value(description, ("Требуемый опыт работы", "Опыт работы", "Опыт"))
        )
        result.append({
            "source": "HeadHunter",
            "external_id": str(external_id),
            "title": title,
            "employer": employer,
            # The feed request is constrained to the exact Sochi area returned by HH /areas.
            "city": area_name,
            "description": full_description,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "currency": currency,
            "salary_type": salary_type,
            "schedule": schedule,
            "experience": experience,
            "published_at": fields.get("pubdate") or fields.get("published") or fields.get("date"),
            "url": url,
        })
    return result

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
        # The anonymous JSON vacancy-search endpoint began returning 403 in 2026.
        # Use HH's public search RSS instead; no applicant account, app key or CAPTCHA bypass.
        headers = {
            "User-Agent": settings.hh_user_agent,
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        }
        if not self._hh_area:
            areas = await self._get_json("https://api.hh.ru/areas", headers=headers)
            self._hh_area = _hh_area_id(areas)
            if not self._hh_area:
                raise RuntimeError("В справочнике HeadHunter не найден город Сочи в Краснодарском крае")

        gathered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for term in SEARCH_TERMS:
            response = await self.client.get(
                "https://hh.ru/search/vacancy/rss",
                params={
                    "text": term,
                    "area": self._hh_area,
                    "clusters": "true",
                    "enable_snippets": "true",
                    "order_by": "publication_time",
                },
                headers=headers,
            )
            response.raise_for_status()
            for item in _parse_hh_rss(response.content, area_name=settings.target_city):
                if item["external_id"] in seen:
                    continue
                seen.add(item["external_id"])
                gathered.append(item)
            await asyncio.sleep(0.25)

        return gathered, (
            "Публичная RSS-лента поиска HeadHunter; JSON API поиска вакансий "
            "для неавторизованных запросов возвращает 403. RSS не требует регистрации "
            "приложения или доступа работодателя; зарплата и график могут отсутствовать "
            "в кратком описании и тогда вакансия попадёт на ручную проверку."
        )

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
