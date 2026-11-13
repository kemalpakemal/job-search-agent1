from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass

# Minimal .env loader so a local copy can be started without extra packages.
def _load_dotenv() -> None:
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)

_load_dotenv()

SEARCH_TERMS = [
    "бухгалтер по расчету заработной платы",
    "бухгалтер по зарплате",
    "бухгалтер расчетчик",
    "ведущий бухгалтер по заработной плате",
    "специалист по расчету заработной платы",
    "специалист по начислению заработной платы",
    "бухгалтер по расчетам с персоналом",
    "бухгалтер расчетной группы",
    "специалист расчетного отдела",
    "бухгалтер по кадровым расчетам",
    "бухгалтер бюджетного учреждения",
    "бухгалтер централизованной бухгалтерии",
    "бухгалтер 1С БГУ",
    "бухгалтер по оплате труда",
]

PROFILE = {
    "candidate_label": "Кандидат",
    "city": "Сочи",
    "experience_years": 4,
    "workplace": "МКУ «ЦБ УОН», централизованная бухгалтерия образовательных учреждений",
    "accounts_count": 300,
    "education": "Среднее профессиональное образование «Экономика и бухгалтерский учёт»; высшее экономическое образование (обучение продолжается)",
    "software": ["1С:БГУ", "Microsoft Excel"],
    "skills": [
        "расчёт и начисление заработной платы",
        "расчёт отпускных, больничных и командировочных",
        "декретные выплаты и пособия по беременности и родам",
        "стандартные налоговые вычеты на детей",
        "табели учёта рабочего времени",
        "зарплатные ведомости и платёжные поручения",
        "справки о доходах и документы по НДФЛ",
        "контроль начислений и удержаний",
        "ежемесячная, квартальная и годовая отчётность по заработной плате",
        "работа с персональными и расчётными данными",
    ],
    "not_confirmed": ["1С:ЗУП", "кадровое делопроизводство полного цикла"],
}

@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/job_agent.db")
    app_password: str = os.getenv("APP_PASSWORD", "")
    min_net_salary: int = int(os.getenv("MIN_NET_SALARY", "60000"))
    target_city: str = os.getenv("TARGET_CITY", "Сочи")
    target_region_code: str = os.getenv("TRUDVSEM_REGION_CODE", "23")
    search_interval_hours: int = int(os.getenv("SEARCH_INTERVAL_HOURS", "12"))
    hh_user_agent: str = os.getenv("HH_USER_AGENT", "JobSearchAgent/1.0 (set-contact-email-in-env)")
    superjob_app_id: str = os.getenv("SUPERJOB_APP_ID", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_allowed_user_ids: str = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")

settings = Settings()
