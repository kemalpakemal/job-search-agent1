from __future__ import annotations

import re
from typing import Any


def normalized(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def estimate_net_from_gross(monthly_gross: float) -> float:
    """Approximate monthly take-home using annualized progressive RF NDFL bands."""
    annual = max(0.0, monthly_gross) * 12
    bands = [(2_400_000, 0.13), (5_000_000, 0.15), (20_000_000, 0.18),
             (50_000_000, 0.20), (float("inf"), 0.22)]
    tax, lower, remaining = 0.0, 0.0, annual
    for upper, rate in bands:
        taxable = min(max(remaining, 0.0), upper - lower)
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


PAYROLL_TITLE_PATTERNS = (
    r"бухгалтер.{0,60}(расчет|начислен|зарплат|заработн|оплат[аы] труда)",
    r"(расчет|начислен).{0,50}(заработн|зарплат|оплат[аы] труда)",
    r"бухгалтер.{0,40}расчет.{0,30}с персоналом",
    r"бухгалтер[- ]расчетчик",
    r"расчетчик.{0,40}(заработн|зарплат)",
    r"специалист.{0,50}(расчет|начислен|зарплат|заработн|оплат[аы] труда)",
    r"специалист.{0,35}по оплате труда",
    r"расчет.{0,30}с персоналом",
)

PAYROLL_DUTY_TERMS = (
    "расчет заработной платы", "расчета заработной платы", "расчету заработной платы",
    "заработной платы", "расчет зарплаты", "расчета зарплаты", "начисление зарплаты",
    "начисление заработной платы", "начислению заработной платы", "начисление оплаты труда",
    "расчет оплаты труда", "расчеты с персоналом", "расчет с персоналом",
    "зарплатн ведомост", "расчет отпускных", "отпускные", "расчет больничных",
    "больничные", "расчет пособий", "табел", "удержан", "ндфл", "выплат сотрудникам",
    "межрасчет",
)

GENERAL_ACCOUNTING_EXCLUSIONS = (
    "главный бухгалтер", "заместитель главного бухгалтера", "зам главного бухгалтера",
    "бухгалтер-эксперт", "бухгалтер эксперт", "бухгалтер-калькулятор",
    "бухгалтер материального стола", "бухгалтер по учету тмц",
    "бухгалтер по первичной документации",
)


def score_vacancy(vacancy: dict[str, Any]) -> tuple[int, list[str]]:
    """Prioritize payroll accounting; generic accounting must not rank as a match."""
    title = normalized(vacancy.get("title"))
    description = normalized(vacancy.get("description"))
    schedule = normalized(vacancy.get("schedule"))
    city = normalized(vacancy.get("city"))
    text = f"{title} {description}"
    reasons: list[str] = []
    score = 0
    title_is_payroll = any(re.search(pattern, title) for pattern in PAYROLL_TITLE_PATTERNS)
    duty_hits = [term for term in PAYROLL_DUTY_TERMS if term in text]
    excluded_general = any(term in title for term in GENERAL_ACCOUNTING_EXCLUSIONS)

    if title_is_payroll:
        # The RSS feed may contain only a short snippet rather than full vacancy duties.
        # An exact payroll-accounting title is strong evidence of candidate-role fit;
        # salary and 5/2 schedule are still checked independently before qualification.
        score += 76
        reasons.append("Название должности прямо соответствует специализации кандидата — расчёт заработной платы")
    elif "бухгалтер" in title or "специалист" in title:
        score += 5
        reasons.append("Название связано с бухгалтерией, но зарплатная специализация не подтверждена")
    else:
        reasons.append("Название должности не подтверждает профиль расчёта заработной платы")

    score += min(32, len(duty_hits) * 8)
    if duty_hits:
        reasons.append("В обязанностях найдены профильные операции: " + ", ".join(duty_hits[:4]))
    else:
        reasons.append("В описании не найдены конкретные обязанности по расчёту зарплаты")

    if "1с:бгу" in text or "1с бгу" in text:
        score += 8
        reasons.append("Упоминается 1С:БГУ")
    elif "1с:зуп" in text or "1с зуп" in text:
        score += 4
        reasons.append("Упоминается 1С:ЗУП; опыт кандидата в этой конфигурации не подтверждён")
    elif "1с" in text:
        score += 2
        reasons.append("Упоминается 1С без уточнения конфигурации")

    if any(term in text for term in ("бюджетн", "школ", "образовательн", "детск", "муниципальн", "государственн")):
        score += 7
        reasons.append("Есть признаки бюджетной или образовательной организации")
    if any(term in text for term in ("excel", "эксель", "отчетност", "отчётност", "табел")):
        score += 4
        reasons.append("Упоминаются Excel, табели или отчётность")
    if any(term in text for term in ("от 1 года", "от 2 лет", "от 3 лет", "4 года", "опыт от 1", "опыт от 2", "опыт от 3")):
        score += 3
        reasons.append("Требование к опыту потенциально соответствует четырём годам кандидата")
    elif any(term in text for term in ("без опыта", "опыт не требуется", "не требуется опыт")):
        score += 3
        reasons.append("Работодатель допускает кандидатов без опыта")
    if any(term in schedule or term in text for term in ("5/2", "пятиднев", "пн-пт", "понедельник-пятница")):
        score += 4
        reasons.append("Упоминается график 5/2")
    if "сочи" in city:
        score += 4
        reasons.append("Локация — Сочи")

    if excluded_general and not title_is_payroll and len(duty_hits) < 2:
        reasons.append("Исключено из профильной выдачи: это общая/главная бухгалтерия, а не расчёт зарплаты")
        return min(score, 24), reasons
    if not title_is_payroll and len(duty_hits) < 2:
        score = min(score, 20)
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
