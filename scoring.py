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


# Candidate profile is grounded in the uploaded resume:
# four years of payroll accounting in a centralized budgetary accounting unit
# serving schools/kindergartens; 1C:BGU, Excel, payroll, sick leave, vacations,
# business trips, maternity benefits, deductions, timesheets and payroll docs.
PAYROLL_TITLE_PATTERNS = (
    r"бухгалтер.{0,60}(расчет|начислен|зарплат|заработн|оплат[аы] труда)",
    r"(расчет|начислен).{0,50}(заработн|зарплат|оплат[аы] труда)",
    r"бухгалтер.{0,45}расчет.{0,35}с персоналом",
    r"бухгалтер[- ]расчетчик",
    r"расчетчик.{0,40}(заработн|зарплат)",
    r"специалист.{0,55}(расчет|начислен|зарплат|заработн|оплат[аы] труда)",
    r"специалист.{0,35}по оплате труда",
    r"расчет.{0,35}с персоналом",
    r"расчетн.{0,25}групп.{0,30}(зарплат|заработн|персонал)",
    r"зарплатн.{0,25}(групп|участок|отдел)",
)

GENERAL_ACCOUNTING_EXCLUSIONS = (
    "главный бухгалтер", "заместитель главного бухгалтера",
    "зам главного бухгалтера", "бухгалтер-эксперт", "бухгалтер эксперт",
    "бухгалтер-калькулятор", "бухгалтер материального стола",
    "бухгалтер по учету тмц", "бухгалтер по учету материалов",
    "бухгалтер по первичной документации", "бухгалтер по реализации",
)

CORE_PAYROLL_TERMS = (
    "расчет заработной платы", "расчета заработной платы",
    "расчету заработной платы", "начисление заработной платы",
    "начислению заработной платы", "расчет зарплаты",
    "расчета зарплаты", "начисление зарплаты", "начислению зарплаты",
    "расчет оплаты труда", "начисление оплаты труда",
    "расчеты с персоналом по оплате труда", "расчетов с персоналом по оплате труда",
)

SUPPORT_TERM_GROUPS = (
    ("отпуск", "отпускн"),
    ("больнич", "листок нетрудоспособности"),
    ("командиров",),
    ("декрет", "беременност", "пособ"),
    ("налогов", "ндфл", "вычет"),
    ("табел", "учет рабочего времени"),
    ("зарплатн ведом", "расчетн ведом", "ведомост на выплат"),
    ("удержан", "выплат сотрудник"),
    ("1с:бгу", "1с бгу", "бгу"),
    ("справк о доход", "2-ндфл", "сведени о доход"),
    ("excel", "эксель"),
)

TRAINING_TERMS = (
    "обучим сами", "обучаем сами", "обучение с нуля", "обучим с нуля",
    "готовы обучить", "готовы обучать", "научим", "обучение на месте",
    "обучение предоставляется", "предусмотрено обучение",
    "без опыта", "опыт не требуется", "не требуется опыт",
    "можно без опыта", "рассмотрим без опыта", "без опыта работы",
    "обучение за счет работодателя", "обучение за счет компании",
)


def has_training_offer(text: str | None) -> bool:
    value = normalized(text)
    return any(term in value for term in TRAINING_TERMS)


def has_experience_conflict(text: str | None) -> bool:
    """Only flag an explicit minimum above the candidate's four years.
    Training / no-experience postings override that flag.
    """
    value = normalized(text)
    if has_training_offer(value):
        return False
    return any(term in value for term in (
        "от 5 лет", "от пяти лет", "не менее 5 лет",
        "не менее пяти лет", "опыт работы 5 лет", "опыт от 5 лет",
        "стаж не менее 5 лет",
    ))


def required_software_conflicts(text: str | None) -> list[str]:
    """Flag employer-required software not confirmed in the candidate's resume.

    The profile confirms 1C:BGU and Excel, but not 1C:ZGU, Parus or the
    Web Budget Execution system. Training offers override these gaps.
    """
    value = normalized(text)
    if has_training_offer(value):
        return []
    conflicts: list[str] = []
    if any(term in value for term in ("1с:згу", "1с згу", "1c:згу", "1c згу")) and not any(
        term in value for term in ("не требуется 1с:згу", "1с:згу не требуется")
    ):
        conflicts.append("Указана 1С:ЗГУ; в резюме подтверждена 1С:БГУ — нужно уточнить, готовы ли обучить")
    if "парус" in value and not any(term in value for term in ("парус не требуется", "без паруса")):
        conflicts.append("Указана программа «Парус»; опыт работы с ней в резюме не подтверждён")
    if any(term in value for term in ("веб исполнение бюджета", "веб-исполнение бюджета", "веб исполнению бюджета")):
        conflicts.append("Указана система «Веб-исполнение бюджета»; опыт работы с ней в резюме не подтверждён")
    return conflicts


def _title_is_payroll(title: str) -> bool:
    return any(re.search(pattern, title) for pattern in PAYROLL_TITLE_PATTERNS)


def is_profile_relevant(vacancy: dict[str, Any]) -> bool:
    title = normalized(vacancy.get("title"))
    description = normalized(vacancy.get("description"))
    if any(term in title for term in GENERAL_ACCOUNTING_EXCLUSIONS):
        return False
    if _title_is_payroll(title):
        return True

    text = f"{title} {description}"
    has_core_task = any(term in text for term in CORE_PAYROLL_TERMS)
    support_hits = sum(
        1 for group in SUPPORT_TERM_GROUPS
        if any(term in text for term in group)
    )
    accounting_role = any(term in title for term in ("бухгалтер", "расчетчик", "расчётчик", "специалист"))
    return bool(accounting_role and has_core_task and support_hits >= 2)


def score_vacancy(vacancy: dict[str, Any]) -> tuple[int, list[str]]:
    """Score against the candidate's actual payroll-accounting background."""
    title = normalized(vacancy.get("title"))
    description = normalized(vacancy.get("description"))
    schedule = normalized(vacancy.get("schedule"))
    experience = normalized(vacancy.get("experience"))
    city = normalized(vacancy.get("city"))
    text = f"{title} {description} {experience}"
    reasons: list[str] = []
    score = 0

    excluded = any(term in title for term in GENERAL_ACCOUNTING_EXCLUSIONS)
    title_match = _title_is_payroll(title)
    core_hits = [term for term in CORE_PAYROLL_TERMS if term in text]
    support_groups = [
        group for group in SUPPORT_TERM_GROUPS
        if any(term in text for term in group)
    ]

    if excluded:
        reasons.append("Должность относится к общему/иному участку бухгалтерии, а не к расчёту зарплаты")
        return 0, reasons

    if title_match:
        score += 78
        reasons.append("Название должности прямо соответствует профилю расчёта заработной платы")
    elif "бухгалтер" in title or "специалист" in title or "расчетчик" in title or "расчётчик" in title:
        score += 8
        reasons.append("Название близко к бухгалтерии/расчётной работе, но профиль нужно подтвердить обязанностями")
    else:
        reasons.append("В названии не подтверждена расчётная бухгалтерская специализация")

    if core_hits:
        score += min(18, 12 + (len(core_hits) - 1) * 3)
        reasons.append("В вакансии прямо указаны расчёт или начисление зарплаты")
    if support_groups:
        score += min(20, len(support_groups) * 4)
        reasons.append("Совпадают обязанности из резюме: " + ", ".join(
            next((name for name in group if name in text), group[0])
            for group in support_groups[:5]
        ))

    if "1с:бгу" in text or "1с бгу" in text:
        score += 7
        reasons.append("Совпадает используемая кандидатом система 1С:БГУ")
    elif any(term in text for term in ("1с:згу", "1с згу")):
        reasons.append("Указана 1С:ЗГУ; в резюме подтверждена другая конфигурация — 1С:БГУ")
    elif "1с:зуп" in text or "1с зуп" in text:
        reasons.append("Указана 1С:ЗУП; в резюме подтверждена другая конфигурация — 1С:БГУ")
    elif "1с" in text:
        score += 2
        reasons.append("Упоминается 1С; конфигурация не уточнена")

    if any(term in text for term in ("бюджетн", "школ", "образовательн", "детск", "муниципальн", "государственн")):
        score += 5
        reasons.append("Есть сходство с бюджетной/образовательной организацией")
    if "excel" in text or "эксель" in text:
        score += 3
        reasons.append("Упоминается Excel")
    if any(term in text for term in ("от 1 года", "от 2 лет", "от 3 лет", "4 года", "опыт от 1", "опыт от 2", "опыт от 3")):
        score += 3
        reasons.append("Требование к стажу укладывается в подтверждённые четыре года")
    if has_training_offer(text):
        score += 5
        reasons.append("Работодатель готов обучать или рассматривает кандидатов без опыта")
    if any(term in schedule or term in text for term in ("5/2", "пятиднев", "пн-пт", "понедельник-пятница")):
        score += 3
        reasons.append("Указан график 5/2")
    if "сочи" in city:
        score += 4
        reasons.append("Локация — Сочи")

    # A vacancy can match the duties almost perfectly while still requiring
    # systems not confirmed in the candidate's CV. Keep it attractive, but do
    # not report a perfect score when required software gaps need clarification.
    software_conflicts = required_software_conflicts(description)
    if software_conflicts:
        deduction = min(30, len(software_conflicts) * 7)
        score = max(0, score - deduction)
        reasons.append(
            f"Снижение оценки на {deduction} баллов: не подтверждён опыт работы "
            f"с обязательным ПО ({len(software_conflicts)} требования)"
        )

    return min(score, 100), reasons


def build_cover_letter(vacancy: dict[str, Any]) -> str:
    title = vacancy.get("title") or "бухгалтер по расчёту заработной платы"
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
        f"С уважением,\nЕкатерина Данилова"
    )
