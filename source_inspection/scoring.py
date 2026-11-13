from __future__ import annotations

import re
from typing import Any


def normalized(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def estimate_net_from_gross(monthly_gross: float) -> float:
    """Estimate monthly take-home using annualized progressive RF NDFL bands.

    This is an estimate only: it ignores personal deductions, other income,
    tax-agent timing and individual circumstances.
    """
    annual = max(0.0, monthly_gross) * 12
    bands = [
        (2_400_000, 0.13),
        (5_000_000, 0.15),
        (20_000_000, 0.18),
        (50_000_000, 0.20),
        (float("inf"), 0.22),
    ]
    tax = 0.0
    lower = 0.0
    remaining = annual
    for upper, rate in bands:
        width = upper - lower
        taxable = min(max(remaining, 0.0), width)
        tax += taxable * rate
        remaining -= taxable
        lower = upper
        if remaining <= 0:
            break
    return max(0.0, (annual - tax) / 12)


def classify_salary_type(raw_text: str, explicit_gross: bool | None = None) -> str:
    if explicit_gross is True:
        return "gross"
    if explicit_gross is False:
        return "net"
    text = normalized(raw_text)
    if any(x in text for x in ("на руки", "net", "после вычета", "чистыми")):
        return "net"
    if any(x in text for x in ("до вычета", "gross", "грязными", "до налогов")):
        return "gross"
    return "unknown"


def salary_decision(salary_min: float | None, salary_max: float | None,
                    currency: str | None, salary_type: str,
                    threshold: int = 60_000) -> tuple[str, float | None, str]:
    if not salary_min or (currency and currency.upper() not in {"RUR", "RUB", "РУБ", "₽"}):
        return "review", None, "Не указана нижняя граница зарплаты или валюта не RUB"
    if salary_type == "net":
        net_min = float(salary_min)
    elif salary_type == "gross":
        net_min = estimate_net_from_gross(float(salary_min))
    else:
        return "review", None, "Неясно, указана зарплата до или после НДФЛ"
    if net_min < threshold:
        return "rejected", net_min, f"Нижняя граница после НДФЛ ниже {threshold:,} ₽".replace(",", " ")
    return "qualified", net_min, "Нижняя граница зарплаты соответствует минимуму на руки"


def score_vacancy(vacancy: dict[str, Any]) -> tuple[int, list[str]]:
    title = normalized(vacancy.get("title"))
    description = normalized(vacancy.get("description"))
    text = f"{title} {description}"
    score = 0
    reasons: list[str] = []

    role_terms = ["бухгалтер", "расчет", "расчетчик", "заработной платы", "зарплат", "оплате труда"]
    role_hits = sum(1 for term in role_terms if term in title)
    role_points = min(30, role_hits * 7 + (5 if "бухгалтер" in title else 0))
    score += role_points
    if role_points:
        reasons.append("Название должности связано с бухгалтерией и расчётом зарплаты")

    payroll_terms = ["заработной платы", "начислен", "расчет зарплат", "отпускн", "больничн", "ндфл", "табел", "удержан"]
    payroll_hits = sum(1 for term in payroll_terms if term in text)
    payroll_points = min(25, payroll_hits * 4)
    score += payroll_points
    if payroll_points:
        reasons.append("В описании есть обязанности по расчётам с персоналом")

    if "1с:бгу" in text or "1с бгу" in text:
        score += 10
        reasons.append("Упоминается 1С:БГУ")
    elif "1с" in text:
        score += 4
        reasons.append("Упоминается 1С, конкретная конфигурация не совпадает или не указана")

    budget_terms = ["бюджетн", "школ", "образовательн", "детск", "муниципальн", "государственн"]
    if any(term in text for term in budget_terms):
        score += 10
        reasons.append("Есть признаки бюджетной или образовательной организации")

    if any(term in text for term in ["excel", "эксель", "сводн", "отчетност", "отчетност"]):
        score += 5
        reasons.append("Упоминаются Excel, отчётность или аналитические таблицы")

    if any(term in text for term in ["без опыта", "не требуется опыт", "опыт не обязателен"]):
        score += 5
        reasons.append("Работодатель допускает кандидатов без опыта")
    elif any(term in text for term in ["от 3 лет", "3 года", "от 2 лет", "2 года", "от 1 года"]):
        score += 5
        reasons.append("Требуемый опыт потенциально соответствует четырём годам кандидата")

    if any(term in text for term in ["5/2", "пятиднев", "пн-пт", "понедельник-пятница"]):
        score += 5
        reasons.append("Упоминается пятидневный график")

    if "сочи" in normalized(vacancy.get("city")):
        score += 5
        reasons.append("Локация — Сочи")
    return min(score, 100), reasons


def build_cover_letter(vacancy: dict[str, Any]) -> str:
    title = vacancy.get("title") or "бухгалтер"
    company = vacancy.get("employer") or "вашей компании"
    description = normalized(vacancy.get("description"))
    focus = "расчёте и начислении заработной платы"
    if "отпуск" in description or "больнич" in description:
        focus = "расчёте заработной платы, отпускных и больничных"
    return (
        f"Здравствуйте!\n\n"
        f"Заинтересовала вакансия «{title}» в {company}. У меня четыре года опыта "
        f"работы ведущим бухгалтером по расчёту заработной платы в централизованной "
        f"бухгалтерии образовательных учреждений. Вела расчёты примерно по 300 "
        f"лицевым счетам сотрудников.\n\n"
        f"Работаю в 1С:БГУ и Excel. Есть практический опыт {focus}, "
        f"оформления командировок и декретных выплат, ведения табелей, подготовки "
        f"зарплатных ведомостей, справок о доходах и отчётности, контроля начислений "
        f"и удержаний.\n\n"
        f"Буду готова подробнее рассказать о своём опыте и ответить на вопросы.\n\n"
        f"С уважением,\nКандидат"
    )
