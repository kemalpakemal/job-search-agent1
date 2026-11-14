from __future__ import annotations

import json
import asyncio
import hashlib
import os
import re
from urllib.parse import urljoin
from html import unescape as html_unescape
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus

import httpx

from config import settings
from scoring import classify_salary_type, is_profile_relevant

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Queries stay inside the candidate's payroll-accounting occupation.
PAYROLL_SEARCH_TERMS = (
    "бухгалтер по расчету заработной платы",
    "бухгалтер по зарплате",
    "ведущий бухгалтер по расчету заработной платы",
    "ведущий бухгалтер по заработной плате",
    "бухгалтер-расчетчик",
    "бухгалтер расчетчик зарплаты",
    "специалист по расчету заработной платы",
    "специалист по начислению заработной платы",
    "бухгалтер по расчетам с персоналом",
    "специалист по оплате труда",
    "расчетная группа заработная плата",
    "бухгалтер по зарплате 1С БГУ",
    "помощник бухгалтера по расчету заработной платы",
    "бухгалтер по расчету зарплаты без опыта",
    "бухгалтер расчетчик обучение",
    "расчетчик заработной платы обучение",
    "бухгалтер по зарплате обучим",
)


def _money(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _structured_text(value: Any) -> str:
    """Normalize text-like values that may be strings or API dictionary objects."""
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("title", "name", "value", "text", "label"):
            if value.get(key) not in (None, ""):
                return _structured_text(value[key])
        return " ".join(
            part for part in (_structured_text(v) for v in value.values()) if part
        )
    if isinstance(value, list):
        return ", ".join(part for part in (_structured_text(v) for v in value) if part)
    return str(value).strip()


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
    match = re.search(rf"(?im)(?:^|\n)\s*(?:{names})\s*[:—-]\s*([^\n]+)", text)
    return match.group(1).strip(" \t.;") if match else ""


def _rss_salary(text: str, separate_salary: str = "") -> tuple[float | None, float | None, str | None, str]:
    # Parse only lines that look like salary information; do not mistake IDs or other numbers for pay.
    lines = [line for line in (separate_salary + "\n" + text).splitlines()
             if any(token in line.lower() for token in ("зарплат", "оплат", "доход", "₽", "руб"))]
    candidate = " ".join(lines)
    pattern = (
        r"(?i)(?:от\s*)?(\d[\d\s\u00a0\u202f]{2,})"
        r"(?:\s*(?:[-–—]|до)\s*(\d[\d\s\u00a0\u202f]{2,}))?"
        r"\s*(?:₽|руб(?:\.|лей|ля|ль)?)"
    )
    match = re.search(pattern, candidate)
    if not match:
        return None, None, None, classify_salary_type(candidate)
    def to_number(value: str | None) -> float | None:
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
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
        id_match = re.search(r"/vacancy/(\d+)", url)
        external_id = id_match.group(1) if id_match else (fields.get("guid") or url)
        if external_id in seen:
            continue
        seen.add(external_id)
        extra_salary = fields.get("salary", "")
        salary_min, salary_max, currency, salary_type = _rss_salary(description, extra_salary)
        full_description = " ".join(part for part in (description, extra_salary) if part).strip()[:12000]
        employer = (
            fields.get("company") or fields.get("employer")
            or _rss_labeled_value(description, ("Вакансия компании", "Компания", "Работодатель", "Организация"))
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

def _parse_hh_initial_state(page: bytes | str) -> dict[str, Any]:
    """Read HeadHunter's server-rendered public page JSON state."""
    page_text = page.decode("utf-8", errors="replace") if isinstance(page, bytes) else page
    pattern = r"""<template[^>]*id=["']HH-Lux-InitialState["'][^>]*>(.*?)</template>"""
    match = re.search(pattern, page_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise RuntimeError("В HTML HeadHunter не найден HH-Lux-InitialState")
    try:
        state = json.loads(html_unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Не удалось разобрать JSON HH-Lux-InitialState") from exc
    if not isinstance(state, dict):
        raise RuntimeError("HeadHunter вернул неизвестную структуру начального состояния")
    return state


def _hh_display(value: Any) -> str:
    """Extract a human-readable label from HH dictionaries/lists without leaking IDs."""
    if isinstance(value, dict):
        for key in ("visibleName", "name", "title", "label", "text", "value"):
            if value.get(key) not in (None, ""):
                return _hh_display(value[key])
        return ""
    if isinstance(value, list):
        return ", ".join(part for part in (_hh_display(v) for v in value) if part)
    return str(value).strip() if value is not None else ""


def _hh_experience(value: Any) -> str:
    codes = {
        "noExperience": "Без опыта",
        "between1And3": "От 1 года до 3 лет",
        "between3And6": "От 3 до 6 лет",
        "moreThan6": "Более 6 лет",
    }
    if isinstance(value, str) and value in codes:
        return codes[value]
    if isinstance(value, dict):
        code = str(value.get("id") or value.get("code") or "")
        if code in codes:
            return codes[code]
    return _hh_display(value)


def _hh_schedule(payload: dict[str, Any]) -> str:
    """Prefer an explicit day pattern (5/2 etc.); otherwise keep HH's work-format label."""
    day_value = (
        payload.get("workScheduleByDays")
        or payload.get("workingDays")
        or payload.get("scheduleByDays")
    )
    day_text = _hh_display(day_value)
    day_codes = {
        "FIVE_ON_TWO_OFF": "5/2",
        "SIX_ON_ONE_OFF": "6/1",
        "TWO_ON_TWO_OFF": "2/2",
        "THREE_ON_THREE_OFF": "3/3",
        "FOUR_ON_TWO_OFF": "4/2",
    }
    raw_codes: list[str] = []

    def collect_codes(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("id", "code", "value"):
                if value.get(key):
                    raw_codes.append(str(value[key]).upper())
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    collect_codes(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_codes(nested)
        elif isinstance(value, str):
            raw_codes.append(value.upper())

    collect_codes(day_value)
    for code in raw_codes:
        if code in day_codes:
            return day_codes[code]
    match = re.search(r"(?<!\d)([23456]\s*/\s*[123456])(?!\d)", day_text)
    if match:
        return re.sub(r"\s", "", match.group(1))

    schedule_value = payload.get("workSchedule") or payload.get("schedule")
    work_codes = {
        "FULL_DAY": "Полный день",
        "SHIFT": "Сменный график",
        "FLEXIBLE": "Гибкий график",
        "REMOTE": "Удалённая работа",
        "FLY_IN_FLY_OUT": "Вахтовый метод",
    }
    if isinstance(schedule_value, str):
        return work_codes.get(schedule_value.upper(), schedule_value)
    return _hh_display(schedule_value)


def _hh_search_item_to_vacancy(item: dict[str, Any], area_name: str) -> dict[str, Any]:
    """Normalize one HH-Lux search-result object into the agent's shared vacancy schema."""
    vacancy_id = str(item.get("vacancyId") or item.get("id") or "").strip()
    company = item.get("company") or item.get("employer") or {}
    area = item.get("area") or {}
    compensation = item.get("compensation") or item.get("salary") or {}
    snippet = item.get("snippet") or {}
    description = _compact_description(
        snippet.get("req") or snippet.get("requirement"),
        snippet.get("resp") or snippet.get("responsibility"),
        snippet.get("cond") or snippet.get("conditions"),
    )
    salary_type = classify_salary_type(
        description,
        explicit_gross=compensation.get("gross") if isinstance(compensation.get("gross"), bool) else None,
    )
    publication = item.get("publicationTime") or item.get("publishedAt") or item.get("published_at")
    if isinstance(publication, dict):
        publication = publication.get("date") or publication.get("text") or publication.get("value") or ""
    links = item.get("links") or {}
    currency = compensation.get("currencyCode") or compensation.get("currency") or "RUR"
    return {
        "source": "HeadHunter",
        "external_id": vacancy_id,
        "title": str(item.get("name") or item.get("title") or "").strip(),
        "employer": _hh_display(company) or "Работодатель не указан",
        "city": _hh_display(area) or area_name,
        "description": description[:12000],
        "salary_min": _money(compensation.get("from")),
        "salary_max": _money(compensation.get("to")),
        "currency": currency,
        "salary_type": salary_type,
        "schedule": _hh_schedule(item),
        "experience": _hh_experience(item.get("experience")),
        "published_at": str(publication or ""),
        "url": links.get("desktop") or links.get("mobile") or item.get("url")
            or (f"https://hh.ru/vacancy/{vacancy_id}" if vacancy_id else ""),
    }


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

    async def _enrich_hh(self, item: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """Replace the short RSS snippet with the full public vacancy card when available."""
        vacancy_id = str(item.get("external_id") or "")
        if not vacancy_id.isdigit():
            return item
        try:
            detail = await self._get_json(
                f"https://api.hh.ru/vacancies/{vacancy_id}",
                headers={**headers, "Accept": "application/json"},
            )
        except Exception:
            return item

        salary = detail.get("salary") or {}
        address = detail.get("address") or {}
        area = detail.get("area") or {}
        employer = detail.get("employer") or {}
        schedule = detail.get("schedule") or {}
        experience = detail.get("experience") or {}
        description = _rss_plain_text(detail.get("description") or "")
        skills = detail.get("key_skills") or []
        skill_names = [str(x.get("name")) for x in skills if isinstance(x, dict) and x.get("name")]
        if skill_names:
            description += "\nНавыки: " + ", ".join(skill_names)
        employment = detail.get("employment") or {}
        if employment.get("name"):
            description += "\nЗанятость: " + str(employment["name"])
        if salary:
            salary_line = "Зарплата: "
            if salary.get("from") is not None:
                salary_line += "от " + str(salary["from"])
            if salary.get("to") is not None:
                salary_line += " до " + str(salary["to"])
            salary_line += " " + str(salary.get("currency") or "RUB")
            salary_line += " до вычета НДФЛ" if salary.get("gross") else " на руки"
            description += "\n" + salary_line

        item.update({
            "description": description[:12000] or item.get("description", ""),
            "employer": employer.get("name") or item.get("employer"),
            "city": (address.get("city") if isinstance(address, dict) else None) or area.get("name") or item.get("city"),
            "salary_min": salary.get("from") if salary.get("from") is not None else item.get("salary_min"),
            "salary_max": salary.get("to") if salary.get("to") is not None else item.get("salary_max"),
            "currency": salary.get("currency") or item.get("currency") or ("RUB" if salary else None),
            "salary_type": classify_salary_type(
                description,
                explicit_gross=salary.get("gross") if isinstance(salary.get("gross"), bool) else None,
            ),
            "schedule": schedule.get("name") or item.get("schedule"),
            "experience": experience.get("name") or item.get("experience"),
            "url": detail.get("alternate_url") or detail.get("url") or item.get("url"),
        })
        return item


    async def hh(self) -> tuple[list[dict[str, Any]], str]:
        """Search HeadHunter public server-rendered pages and enrich matching vacancies.

        The applicant-facing JSON API can return 403. The public search page still
        exposes structured vacancy cards in HH-Lux-InitialState, including salary,
        employer and experience. Full descriptions are available on the public
        vacancy page's vacancyView object.
        """
        if not settings.hh_user_agent or "set-contact-email-in-env" in settings.hh_user_agent.lower():
            raise RuntimeError(
                "HeadHunter не запрашивался: укажи HH_USER_AGENT с названием приложения "
                "и контактным email."
            )
        headers = {
            "User-Agent": settings.hh_user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        }
        if not self._hh_area:
            areas = await self._get_json(
                "https://api.hh.ru/areas",
                headers={"User-Agent": settings.hh_user_agent, "Accept": "application/json"},
            )
            self._hh_area = _hh_area_id(areas)
            if not self._hh_area:
                raise RuntimeError("В справочнике HeadHunter не найден город Сочи в Краснодарском крае")

        hh_terms = (
            "бухгалтер по расчету заработной платы",
            "бухгалтер по зарплате",
            "ведущий бухгалтер по расчету заработной платы",
            "бухгалтер-расчетчик",
            "специалист по расчету заработной платы",
            "бухгалтер по расчетам с персоналом",
            "специалист по оплате труда",
            "расчетчик заработной платы",
            "бухгалтер по зарплате без опыта",
            "бухгалтер расчетчик обучение",
            "помощник бухгалтера по расчету заработной платы",
        )
        search_semaphore = asyncio.Semaphore(2)

        async def fetch_term(term: str) -> list[dict[str, Any]]:
            async with search_semaphore:
                params = {
                    "text": term,
                    "area": self._hh_area,
                    "enable_snippets": "true",
                    "clusters": "true",
                    "ored_clusters": "true",
                    "page": 0,
                    "per_page": 50,
                }
                response = await self.client.get(
                    "https://hh.ru/search/vacancy",
                    params=params,
                    headers=headers,
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(2)
                    response = await self.client.get(
                        "https://hh.ru/search/vacancy",
                        params=params,
                        headers=headers,
                    )
                response.raise_for_status()
                state = _parse_hh_initial_state(response.content)
                result = state.get("vacancySearchResult") or {}
                rows = result.get("vacancies") or []
                if not isinstance(rows, list):
                    raise RuntimeError("HH-Lux-InitialState не содержит список вакансий")
                normalized: list[dict[str, Any]] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    vacancy = _hh_search_item_to_vacancy(row, settings.target_city)
                    if vacancy["external_id"] and vacancy["title"]:
                        normalized.append(vacancy)
                return normalized

        results = await asyncio.gather(
            *(fetch_term(term) for term in hh_terms),
            return_exceptions=True,
        )
        unique: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        successful_queries = 0
        for term, result in zip(hh_terms, results):
            if isinstance(result, BaseException):
                failures.append(f"{term}: {type(result).__name__}: {str(result)[:180]}")
                continue
            successful_queries += 1
            for item in result:
                unique.setdefault(item["external_id"], item)

        # If the HTML state changes or HH blocks the page, retain the RSS route
        # as a transparent fallback rather than disabling the whole source.
        if successful_queries == 0:
            rss_headers = {
                "User-Agent": settings.hh_user_agent,
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
            }
            rss_results = await asyncio.gather(
                *(
                    self.client.get(
                        "https://hh.ru/search/vacancy/rss",
                        params={"text": term, "area": self._hh_area, "enable_snippets": "true"},
                        headers=rss_headers,
                    )
                    for term in hh_terms
                ),
                return_exceptions=True,
            )
            rss_ok = 0
            for result in rss_results:
                if isinstance(result, BaseException):
                    continue
                try:
                    result.raise_for_status()
                    rss_ok += 1
                    for item in _parse_hh_rss(result.content, area_name=settings.target_city):
                        unique.setdefault(item["external_id"], item)
                except Exception:
                    continue
            if rss_ok == 0:
                details = " | ".join(failures[:3])
                raise RuntimeError(
                    f"Не удалось получить выдачу HeadHunter через HTML и RSS. {details}"
                )
            search_mode = f"RSS fallback: {rss_ok}/{len(hh_terms)}"
        else:
            search_mode = f"HTML HH-Lux-InitialState: {successful_queries}/{len(hh_terms)}"

        # The search state already carries structured salary/experience/company
        # fields. Fetch full vacancy pages only for payroll-title matches.
        candidates = [
            item for item in unique.values()
            if is_profile_relevant({"title": item.get("title"), "description": item.get("description")})
            and re.search(r"/vacancy/(\d+)", item.get("url", ""))
        ][:80]
        detail_semaphore = asyncio.Semaphore(4)
        detail_failures = 0
        detail_success = 0
        detail_error_samples: list[str] = []

        async def enrich_from_page(item: dict[str, Any]) -> dict[str, Any]:
            nonlocal detail_failures, detail_success
            vacancy_id = item["external_id"]
            try:
                async with detail_semaphore:
                    response = await self.client.get(
                        f"https://hh.ru/vacancy/{vacancy_id}",
                        headers=headers,
                    )
                    if response.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1.5)
                        response = await self.client.get(
                            f"https://hh.ru/vacancy/{vacancy_id}",
                            headers=headers,
                        )
                    response.raise_for_status()
                state = _parse_hh_initial_state(response.content)
                view = state.get("vacancyView") or {}
                if not isinstance(view, dict):
                    raise RuntimeError("В карточке HH не найден vacancyView")
                nested = view.get("vacancy") if isinstance(view.get("vacancy"), dict) else {}
                detail = {**view, **nested}
                raw_description = detail.get("description") or view.get("description")
                description = _rss_plain_text(raw_description or "")
                skills = detail.get("keySkills") or detail.get("key_skills") or []
                skill_names = [_hh_display(x) for x in skills]
                skill_names = [x for x in skill_names if x]
                if skill_names:
                    description += "\nНавыки: " + ", ".join(skill_names)
                if description:
                    item["description"] = description[:12000]
                    detail_success += 1
                employer = detail.get("company") or detail.get("employer")
                if employer:
                    item["employer"] = _hh_display(employer) or item["employer"]
                area = detail.get("area")
                if area:
                    item["city"] = _hh_display(area) or item["city"]
                compensation = detail.get("compensation") or detail.get("salary") or {}
                if isinstance(compensation, dict):
                    item["salary_min"] = _money(compensation.get("from")) if compensation.get("from") is not None else item["salary_min"]
                    item["salary_max"] = _money(compensation.get("to")) if compensation.get("to") is not None else item["salary_max"]
                    item["currency"] = compensation.get("currencyCode") or compensation.get("currency") or item["currency"]
                    gross = compensation.get("gross")
                    if isinstance(gross, bool):
                        item["salary_type"] = "gross" if gross else "net"
                exp = detail.get("experience")
                if exp:
                    item["experience"] = _hh_experience(exp) or item["experience"]
                schedule = _hh_schedule(detail)
                if schedule:
                    item["schedule"] = schedule
                page_text = _rss_plain_text(response.text)
                if not item.get("schedule") or item.get("schedule") == "Полный день":
                    match = re.search(
                        r"(?i)(?:график работы|рабочие дни|режим работы)\s*[:—-]?\s*(5\s*/\s*2|2\s*/\s*2|3\s*/\s*3|6\s*/\s*1)",
                        page_text,
                    )
                    if match:
                        item["schedule"] = re.sub(r"\s", "", match.group(1))
                if "зарплата" not in item.get("description", "").lower():
                    salary_line = ""
                    if item.get("salary_min") is not None:
                        salary_line = "Зарплата: от " + str(round(item["salary_min"]))
                        if item.get("salary_max") is not None:
                            salary_line += " до " + str(round(item["salary_max"]))
                        salary_line += " " + str(item.get("currency") or "RUR")
                        if item.get("salary_type") in {"gross", "net"}:
                            salary_line += " до вычета НДФЛ" if item["salary_type"] == "gross" else " на руки"
                    if salary_line:
                        item["description"] = (item.get("description", "") + "\n" + salary_line)[:12000]
            except httpx.HTTPStatusError as exc:
                detail_failures += 1
                if len(detail_error_samples) < 2:
                    detail_error_samples.append(
                        f"HTTP {exc.response.status_code} при загрузке карточки"
                    )
            except Exception as exc:
                detail_failures += 1
                if len(detail_error_samples) < 2:
                    detail_error_samples.append(
                        f"{type(exc).__name__}: {str(exc)[:180]}"
                    )
            return item

        if candidates:
            await asyncio.gather(*(enrich_from_page(item) for item in candidates))

        note = (
            f"{search_mode}: уникальных карточек — {len(unique)}, "
            f"полных карточек проверено — {len(candidates)}, "
            f"полные описания получены — {detail_success}."
        )
        if detail_failures:
            note += f" Не удалось обогатить карточек: {detail_failures}."
            if detail_error_samples:
                note += " Причины: " + " | ".join(detail_error_samples) + "."
        if failures:
            note += f" HTML-ошибок поиска: {len(failures)}; пример: {failures[0][:220]}."
        return list(unique.values()), note

    async def superjob(self) -> tuple[list[dict[str, Any]], str]:
        """Search SuperJob using its official API and the configured application key."""
        app_id = settings.superjob_app_id.strip()
        if not app_id:
            return [], "Не подключён: нужен ключ приложения SUPERJOB_APP_ID из кабинета разработчика SuperJob"

        headers = {"X-Api-App-Id": app_id, "Accept": "application/json"}
        if self._sj_town is None:
            towns = await self._get_json(
                "https://api.superjob.ru/2.0/towns/",
                params={"keyword": settings.target_city, "count": 20},
                headers=headers,
            )
            objects = towns.get("objects", []) if isinstance(towns, dict) else []
            exact = [
                row for row in objects
                if isinstance(row, dict)
                and str(row.get("title") or "").strip().lower() == settings.target_city.lower()
            ]
            chosen = exact[0] if exact else None
            if chosen and chosen.get("id") is not None:
                self._sj_town = int(chosen["id"])
            elif len(objects) == 1 and isinstance(objects[0], dict) and objects[0].get("id") is not None:
                self._sj_town = int(objects[0]["id"])
        if self._sj_town is None:
            raise RuntimeError("Не удалось однозначно определить город Сочи в справочнике SuperJob")

        semaphore = asyncio.Semaphore(3)

        async def fetch_term(term: str) -> list[dict[str, Any]]:
            async with semaphore:
                data = await self._get_json(
                    "https://api.superjob.ru/2.0/vacancies/",
                    params={
                        "keyword": term,
                        "town": self._sj_town,
                        "count": 30,
                        "page": 0,
                    },
                    headers=headers,
                )
            return data.get("objects", []) if isinstance(data, dict) else []

        query_results = await asyncio.gather(
            *(fetch_term(term) for term in PAYROLL_SEARCH_TERMS),
            return_exceptions=True,
        )
        unique: dict[str, dict[str, Any]] = {}
        failed_queries = 0
        for result in query_results:
            if isinstance(result, BaseException):
                failed_queries += 1
                continue
            for item in result:
                if not isinstance(item, dict):
                    continue
                vacancy_id = str(item.get("id") or "").strip()
                title = str(item.get("profession") or "").strip()
                if not vacancy_id or not title or vacancy_id in unique:
                    continue
                description = _compact_description(
                    item.get("candidat"),
                    item.get("work"),
                    item.get("vacancyRichText"),
                )
                town = item.get("town") or {}
                work_type = item.get("type_of_work") or {}
                experience = item.get("experience") or {}
                unique[vacancy_id] = {
                    "source": "SuperJob",
                    "external_id": vacancy_id,
                    "title": title,
                    "employer": item.get("firm_name") or "Работодатель не указан",
                    "city": town.get("title") if isinstance(town, dict) else settings.target_city,
                    "description": description,
                    "salary_min": _money(item.get("payment_from")),
                    "salary_max": _money(item.get("payment_to")),
                    "currency": item.get("currency") or "rub",
                    # Do not assume that the API's payment fields are NET or GROSS.
                    "salary_type": classify_salary_type(description),
                    "schedule": work_type.get("title", "") if isinstance(work_type, dict) else str(work_type or ""),
                    "experience": experience.get("title", "") if isinstance(experience, dict) else str(experience or ""),
                    "published_at": item.get("date_published") or item.get("date_pub"),
                    "url": item.get("link") or "",
                }

        if not unique and failed_queries == len(PAYROLL_SEARCH_TERMS):
            raise RuntimeError("Все поисковые запросы SuperJob завершились ошибкой")

        note = (
            f"Официальный API SuperJob: профильных запросов — {len(PAYROLL_SEARCH_TERMS)}, "
            f"уникальных карточек — {len(unique)}."
        )
        if failed_queries:
            note += f" Не удалось выполнить запросов: {failed_queries}."
        return list(unique.values()), note

    async def avito(self) -> tuple[list[dict[str, Any]], str]:
        """Search public Avito Work vacancies using the documented Job API."""
        client_id = os.getenv("AVITO_CLIENT_ID", "").strip()
        client_secret = os.getenv("AVITO_CLIENT_SECRET", "").strip()
        location_id = os.getenv("AVITO_LOCATION_ID", "634450").strip()
        try:
            max_pages = max(1, min(50, int(os.getenv("AVITO_MAX_PAGES", "10"))))
        except ValueError:
            max_pages = 10

        if not client_id or not client_secret:
            return [], (
                "API Авито не подключён: добавьте AVITO_CLIENT_ID и AVITO_CLIENT_SECRET. "
                "Для Сочи используется AVITO_LOCATION_ID=634450 (можно переопределить)."
            )

        token_response = await self.client.post(
            "https://api.avito.ru/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise RuntimeError("Avito API не вернул access_token")
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        # The business_area catalogue is dynamic. The old static code 10181 is deprecated.
        dictionary_response = await self.client.get(
            "https://api.avito.ru/job/v2/vacancy/dict/business_area",
            headers=headers,
        )
        dictionary_response.raise_for_status()
        dictionary_payload = dictionary_response.json()
        if isinstance(dictionary_payload, dict):
            dictionary_rows = (
                dictionary_payload.get("values")
                or dictionary_payload.get("items")
                or dictionary_payload.get("data")
                or []
            )
        else:
            dictionary_rows = dictionary_payload
        if not isinstance(dictionary_rows, list):
            raise RuntimeError("Avito API вернул неизвестный формат справочника business_area")

        business_areas: list[tuple[int, str]] = []
        for row in dictionary_rows:
            if not isinstance(row, dict) or row.get("deprecated") is True:
                continue
            name = str(row.get("name") or "").strip()
            name_norm = name.lower().replace("ё", "е")
            area_id = row.get("id")
            if area_id is None:
                continue
            if any(term in name_norm for term in ("бухгалтер", "финанс", "банк")):
                try:
                    business_areas.append((int(area_id), name))
                except (TypeError, ValueError):
                    continue
        if not business_areas:
            available = ", ".join(
                str(row.get("name")) for row in dictionary_rows
                if isinstance(row, dict) and row.get("name")
            )
            raise RuntimeError(
                "В актуальном справочнике Avito не найден действующий раздел "
                f"«Бухгалтерия/Финансы/Банки». Доступные значения: {available[:500]}"
            )

        gathered: list[dict[str, Any]] = []
        seen: set[str] = set()
        total_pages_checked = 0
        category_notes: list[str] = []

        # The documented vacancy-search endpoint supports only location, business_area,
        # per_page and page. It does not accept a text query parameter.
        for business_area_id, business_area_name in business_areas:
            page = 1
            pages_reported = 1
            category_count = 0
            while page <= min(max_pages, pages_reported):
                response = await self.client.get(
                    "https://api.avito.ru/job/v2/vacancies",
                    params={
                        "location": location_id,
                        "business_area": business_area_id,
                        "per_page": 100,
                        "page": page,
                    },
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("vacancies", []) if isinstance(payload, dict) else []
                meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
                try:
                    pages_reported = max(1, int(meta.get("pages") or 1))
                except (TypeError, ValueError):
                    pages_reported = 1

                for row in rows if isinstance(rows, list) else []:
                    if not isinstance(row, dict):
                        continue
                    title = str(row.get("title") or row.get("profession") or "").strip()
                    if not title:
                        continue

                    address_details = row.get("addressDetails") or {}
                    if not isinstance(address_details, dict):
                        address_details = {}
                    city = _structured_text(address_details.get("city")) or settings.target_city
                    address = _structured_text(address_details.get("address"))
                    raw_link = str(row.get("link") or "").strip()
                    link = urljoin("https://www.avito.ru/", raw_link) if raw_link else ""
                    if not link:
                        continue

                    external_id_match = re.search(r"(\d{7,})(?:\D*)$", raw_link)
                    external_id = (
                        external_id_match.group(1)
                        if external_id_match
                        else hashlib.sha256(link.encode("utf-8")).hexdigest()
                    )
                    if external_id in seen:
                        continue
                    seen.add(external_id)

                    profession = _structured_text(row.get("profession"))
                    company = _structured_text(row.get("companyName")) or "Работодатель не указан"
                    category = _structured_text(row.get("businessArea")) or business_area_name

                    # Search cards can be sparse. Retain structured fields when present;
                    # never infer missing salary, schedule, experience or tax treatment.
                    params_obj = row.get("params") or {}
                    if not isinstance(params_obj, dict):
                        params_obj = {}
                    salary_obj = params_obj.get("salary") or row.get("salary") or {}
                    if not isinstance(salary_obj, dict):
                        salary_obj = {}
                    salary_min = _money(
                        salary_obj.get("from") or salary_obj.get("min") or row.get("salary_from")
                    )
                    salary_max = _money(
                        salary_obj.get("to") or salary_obj.get("max") or row.get("salary_to")
                    )
                    currency = salary_obj.get("currency") or row.get("currency")
                    schedule = _structured_text(params_obj.get("schedule") or row.get("schedule"))
                    experience = _structured_text(params_obj.get("experience") or row.get("experience"))
                    detail_description = _structured_text(
                        row.get("description") or row.get("text") or row.get("vacancyDescription")
                    )
                    description_parts = [
                        detail_description,
                        f"Профессия по классификатору Авито: {profession}" if profession else "",
                        f"Сфера Авито: {category}",
                        f"Адрес: {address}" if address else "",
                    ]
                    if not detail_description:
                        description_parts.append(
                            "Внимание: поисковая карточка Авито не содержит полного описания обязанностей. "
                            "Проверьте условия на странице вакансии."
                        )

                    gathered.append({
                        "source": "Авито Работа",
                        "external_id": external_id,
                        "title": title,
                        "employer": company,
                        "city": city,
                        "description": "\n".join(part for part in description_parts if part)[:12000],
                        "salary_min": salary_min,
                        "salary_max": salary_max,
                        "currency": currency,
                        "salary_type": classify_salary_type(detail_description),
                        "schedule": schedule,
                        "experience": experience,
                        "published_at": row.get("createdAt") or row.get("publishedAt") or row.get("created"),
                        "url": link,
                    })
                    category_count += 1

                total_pages_checked += 1
                if page >= pages_reported or page >= max_pages:
                    break
                page += 1
                await asyncio.sleep(0.12)

            category_notes.append(
                f"{business_area_name}: получено {category_count} карточек; "
                f"просмотрено до {min(pages_reported, max_pages)} страниц"
            )

        return gathered, (
            "Официальный Avito Job API: поиск по опубликованным вакансиям Сочи "
            "в актуальных категориях бухгалтерии/финансов. Выдача дедуплицирована. "
            "Поисковый метод возвращает упрощённые карточки; подробная карточка вакансии "
            "через API доступна только владельцу объявления, поэтому отсутствующие поля "
            "остаются на ручную проверку. "
            + "; ".join(category_notes)
        )

    async def trudvsem(self) -> tuple[list[dict[str, Any]], str]:
        gathered: list[dict[str, Any]] = []
        base = "https://opendata.trudvsem.ru/api/v1/vacancies/region/" + quote_plus(settings.target_region_code)
        for term in PAYROLL_SEARCH_TERMS:
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
        """Run all supported APIs independently; a failed source must not block others."""
        all_items: list[dict[str, Any]] = []
        statuses: dict[str, str] = {}
        sources = (
            ("HeadHunter", self.hh),
            ("SuperJob", self.superjob),
            ("Работа России", self.trudvsem),
            ("Авито Работа", self.avito),
        )

        async def run_source(name: str, func: Any) -> tuple[str, list[dict[str, Any]], str]:
            try:
                items, note = await func()
                return name, items, f"Получено {len(items)} вакансий. {note}"
            except httpx.HTTPStatusError as exc:
                return name, [], f"Ошибка HTTP {exc.response.status_code}: {exc.response.text[:250]}"
            except Exception as exc:
                return name, [], f"Ошибка подключения/обработки: {type(exc).__name__}: {str(exc)[:250]}"

        results = await asyncio.gather(
            *(run_source(name, func) for name, func in sources)
        )
        for name, items, status in results:
            all_items.extend(items)
            statuses[name] = status
        statuses["Зарплата.ру"] = "Ручной источник: публичный API поиска вакансий для соискателей не подтверждён; скрытый сбор не используется"
        statuses["Работа.ру"] = "Ручной источник: автоматизация допустима только через официальный API и в пределах выданных прав"
        statuses["Сайты работодателей"] = "Ручной источник: можно подключать по отдельности при наличии официального фида/API"
        return all_items, statuses
