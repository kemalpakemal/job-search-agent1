from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import desc, func, select, or_
from sqlalchemy.orm import Session

from config import PROFILE, settings
from connectors import VacancyConnectors
from database import AppSetting, SearchRun, SessionLocal, Vacancy
from scoring import (build_cover_letter, salary_decision, score_vacancy, is_profile_relevant, has_experience_conflict, has_training_offer)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# HTTPX logs full Telegram Bot API URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("job-agent")
scheduled_task: asyncio.Task | None = None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


security = HTTPBasic(auto_error=False)


def basic_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    if not settings.app_password:
        return True
    if not credentials or credentials.username != "user" or not secrets.compare_digest(credentials.password, settings.app_password):
        raise HTTPException(status_code=401, detail="Требуется пароль приложения", headers={"WWW-Authenticate": "Basic"})
    return True


async def send_telegram(text: str) -> None:
    if not settings.telegram_bot_token:
        return
    chat_id = settings.telegram_chat_id.strip()
    if not chat_id:
        # The Telegram bot stores the authorized private chat after /start.
        db = SessionLocal()
        try:
            saved = db.get(AppSetting, "telegram_chat_id")
            chat_id = saved.value if saved else ""
        finally:
            db.close()
    if not chat_id:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            await client.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        except Exception as exc:
            log.warning("Telegram notification failed (%s)", type(exc).__name__)


def setting_value(db: Session, key: str, default: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else default


def safe_city(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("title") or value.get("text") or "")
    return str(value or "")


def evaluate_vacancy(raw: dict[str, Any], threshold: int) -> tuple[int, list[str], float | None, str, str]:
    """One consistent decision path for fresh vacancies and re-scoring."""
    score, score_reasons = score_vacancy(raw)
    if not is_profile_relevant(raw):
        reason = "Не соответствует резюме: работа не связана напрямую с расчётом заработной платы"
        return score, score_reasons, None, "rejected", reason

    salary_bucket, net_min, salary_reason = salary_decision(
        raw.get("salary_min"), raw.get("salary_max"), raw.get("currency"),
        raw.get("salary_type") or "unknown", threshold
    )
    city = safe_city(raw.get("city"))
    city_ok = "сочи" in city.lower()
    city_unknown = not city.strip()
    schedule_text = str(raw.get("schedule") or "").lower()
    schedule_known_ok = any(
        marker in schedule_text
        for marker in ("5/2", "пятиднев", "пн-пт", "понедельник-пятница", "понедельник по пятницу")
    )
    schedule_conflict = any(
        marker in schedule_text
        for marker in ("2/2", "3/3", "сутки через", "вахт", "сменный график", "свободный график")
    )
    experience_text = f"{raw.get('experience') or ''} {raw.get('description') or ''}"
    training = has_training_offer(experience_text)
    experience_conflict = has_experience_conflict(experience_text)

    review_reasons: list[str] = []
    if not city_ok:
        review_reasons.append("Город в вакансии не указан" if city_unknown else "Вакансия указана не в Сочи")
    if salary_reason:
        review_reasons.append(salary_reason)
    if schedule_conflict:
        review_reasons.append("Указан график, отличный от требуемого 5/2")
    elif not schedule_known_ok:
        review_reasons.append("График 5/2 не подтверждён в данных вакансии")
    if training:
        score_reasons.append("Работодатель предлагает обучение или готов рассматривать без опыта")
    elif experience_conflict:
        review_reasons.append("В вакансии указано требование от 5 лет опыта; у кандидата подтверждено 4 года")

    if salary_bucket == "rejected":
        bucket = "rejected"
    elif city and not city_ok:
        bucket = "rejected"
    elif schedule_conflict:
        bucket = "rejected"
    elif salary_bucket == "qualified" and city_ok and schedule_known_ok and not experience_conflict:
        bucket = "qualified"
    else:
        bucket = "review"

    return score, score_reasons, net_min, bucket, "; ".join(review_reasons)


def rescore_existing_review_vacancies() -> dict[str, int]:
    """Re-evaluate unseen vacancies using the complete candidate-profile rules."""
    db = SessionLocal()
    checked = promoted = rejected = 0
    try:
        threshold = int(setting_value(db, "min_net_salary", str(settings.min_net_salary)))
        rows = db.scalars(
            select(Vacancy).where(
                Vacancy.bucket.in_(["review", "qualified"]),
                or_(
                    Vacancy.user_status.is_(None),
                    Vacancy.user_status.in_(["new", "drafted"]),
                ),
            )
        ).all()
        for job in rows:
            raw = {
                "title": job.title,
                "description": job.description,
                "city": job.city,
                "schedule": job.schedule,
                "salary_min": job.salary_min,
                "salary_max": job.salary_max,
                "currency": job.currency,
                "salary_type": job.salary_type,
                "experience": job.experience,
            }
            score, reasons, net_min, bucket, review_reason = evaluate_vacancy(raw, threshold)
            job.score = score
            job.score_reasons = "\n".join(reasons)
            job.net_min_estimate = net_min
            job.bucket = bucket
            job.review_reason = review_reason
            checked += 1
            if bucket == "qualified":
                promoted += 1
            elif bucket == "rejected":
                rejected += 1

        db.commit()
        return {"checked": checked, "promoted": promoted, "rejected": rejected}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_search(notify: bool = True) -> dict[str, Any]:
    db = SessionLocal()
    run = SearchRun()
    db.add(run)
    db.commit()
    db.refresh(run)
    connector = VacancyConnectors()
    inserted = qualified = review = 0
    new_qualified_ids: list[int] = []
    try:
        items, source_status = await connector.run_all()
        threshold = int(setting_value(db, "min_net_salary", str(settings.min_net_salary)))
        for raw in items:
            external_id = str(raw.get("external_id") or "")
            if not external_id:
                # Stable fallback for feeds that omit IDs.
                import hashlib
                external_id = hashlib.sha256((raw.get("url", "") + raw.get("title", "") + raw.get("employer", "")).encode()).hexdigest()
            source = raw.get("source") or "unknown"
            existing = db.scalar(select(Vacancy).where(Vacancy.source == source, Vacancy.external_id == external_id))
            now = datetime.now(timezone.utc)
            if existing:
                existing.last_seen = now
                continue
            city = safe_city(raw.get("city"))
            raw["city"] = city
            score, reasons, net_min, bucket, review_reason = evaluate_vacancy(raw, threshold)
            job = Vacancy(
                source=source, external_id=external_id,
                title=str(raw.get("title") or "Без названия")[:500],
                employer=str(raw.get("employer") or "Работодатель не указан")[:500],
                city=city[:300], description=str(raw.get("description") or "")[:12000],
                salary_min=raw.get("salary_min"), salary_max=raw.get("salary_max"),
                currency=raw.get("currency"), salary_type=raw.get("salary_type") or "unknown",
                net_min_estimate=net_min, schedule=str(raw.get("schedule") or "")[:200],
                experience=str(raw.get("experience") or "")[:200],
                published_at=str(raw.get("published_at") or "")[:80], url=str(raw.get("url") or ""),
                score=score, score_reasons="\n".join(reasons), bucket=bucket,
                review_reason=review_reason, cover_letter=build_cover_letter(raw),
                first_seen=now, last_seen=now,
            )
            db.add(job)
            db.flush()
            inserted += 1
            if bucket == "qualified":
                qualified += 1
                new_qualified_ids.append(job.id)
            elif bucket == "review":
                review += 1
        run.received = len(items)
        run.inserted = inserted
        run.qualified = qualified
        run.review = review
        run.source_status = json.dumps(source_status, ensure_ascii=False)
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        if notify and qualified and new_qualified_ids:
            fresh = db.scalars(select(Vacancy).where(Vacancy.id.in_(new_qualified_ids)).order_by(desc(Vacancy.first_seen)).limit(5)).all()
            lines = [f"Поиск завершён. Новых подходящих вакансий в этом запуске: {qualified}. Всего новых: {inserted}.", "Отклики не отправлялись — только подготовка и сохранение."]
            for item in fresh:
                lines.append(f"\n{item.title} — {item.employer}\nNET от: {round(item.net_min_estimate or 0):,} ₽; соответствие: {item.score}/100\n{item.url}".replace(",", " "))
            await send_telegram("\n".join(lines))
        return {"received": len(items), "inserted": inserted, "qualified": qualified, "review": review, "sources": source_status}
    except Exception as exc:
        db.rollback()
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        db.add(run)
        db.commit()
        log.exception("Search failed")
        raise
    finally:
        await connector.close()
        db.close()


async def scheduled_search_loop():
    # Built-in asyncio scheduler: no external scheduler service is required.
    # It runs only while this single application process is alive.
    while True:
        await asyncio.sleep(max(1, settings.search_interval_hours) * 3600)
        try:
            await run_search()
        except Exception:
            log.exception("Scheduled search failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduled_task
    telegram_app = None
    try:
        rescore_result = rescore_existing_review_vacancies()
        log.info(
            "Existing vacancy re-score finished: checked=%s promoted=%s rejected=%s",
            rescore_result["checked"], rescore_result["promoted"], rescore_result["rejected"]
        )
    except Exception:
        log.exception("Could not re-score existing review vacancies; continuing startup")
    # Run one web worker only; scheduler and Telegram polling share this process.
    if settings.telegram_bot_token:
        try:
            from telegram_bot import start_telegram_bot
            telegram_app = await start_telegram_bot(run_search)
        except Exception:
            log.exception("Telegram bot initialization failed; web application will continue")
    if settings.search_interval_hours > 0:
        scheduled_task = asyncio.create_task(scheduled_search_loop())
    try:
        yield
    finally:
        if scheduled_task:
            scheduled_task.cancel()
            try:
                await scheduled_task
            except asyncio.CancelledError:
                pass
        if telegram_app:
            try:
                from telegram_bot import stop_telegram_bot
                await stop_telegram_bot()
            except Exception:
                log.exception("Telegram bot shutdown failed")


app = FastAPI(title="Агент поиска работы — Сочи", version="1.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index(_: bool = Depends(basic_auth)):
    html = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"ok": True, "app": "job-search-agent", "version": app.version}


@app.get("/api/profile")
async def profile(_: bool = Depends(basic_auth)):
    return PROFILE


@app.get("/api/stats")
async def stats(db: Session = Depends(get_db), _: bool = Depends(basic_auth)):
    total = db.scalar(select(func.count(Vacancy.id))) or 0
    qualified = db.scalar(select(func.count(Vacancy.id)).where(Vacancy.bucket == "qualified")) or 0
    review = db.scalar(select(func.count(Vacancy.id)).where(Vacancy.bucket == "review")) or 0
    rejected = db.scalar(select(func.count(Vacancy.id)).where(Vacancy.bucket == "rejected")) or 0
    latest = db.scalar(select(SearchRun).order_by(desc(SearchRun.id)).limit(1))
    return {"total": total, "qualified": qualified, "review": review, "rejected": rejected,
            "last_search": latest.finished_at.isoformat() if latest and latest.finished_at else None,
            "search_interval_hours": settings.search_interval_hours,
            "auto_apply_enabled": False}


@app.get("/api/sources")
async def sources(db: Session = Depends(get_db), _: bool = Depends(basic_auth)):
    latest = db.scalar(select(SearchRun).order_by(desc(SearchRun.id)).limit(1))
    statuses = json.loads(latest.source_status) if latest and latest.source_status else {}
    statuses.setdefault("HeadHunter", "Ожидает запуска поиска")
    statuses.setdefault("SuperJob", "Не подключён: требуется API-ключ приложения")
    statuses.setdefault("Работа России", "Ожидает запуска поиска")
    statuses.setdefault("Зарплата.ру", "Ручной поиск")
    statuses.setdefault("Работа.ру", "Ручной поиск")
    statuses.setdefault("Авито Работа", "Ручной поиск")
    statuses.setdefault("Сайты работодателей", "Ручное добавление ссылок")
    saved_chat = db.get(AppSetting, "telegram_chat_id")
    return {"sources": statuses, "secrets_configured": {
        "superjob_app_id": bool(settings.superjob_app_id),
        "telegram": bool(settings.telegram_bot_token),
        "telegram_chat_configured": bool(settings.telegram_chat_id or (saved_chat and saved_chat.value)),
        "llm": bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model),
        "hh_user_agent_customized": "set-contact-email-in-env" not in settings.hh_user_agent,
    }, "note": "Автоматические отклики выключены. Источники с ручным статусом не сканируются автоматически."}


@app.get("/api/vacancies")
async def vacancies(bucket: str = "qualified", q: str = "", limit: int = 200,
                    db: Session = Depends(get_db), _: bool = Depends(basic_auth)):
    stmt = select(Vacancy).order_by(desc(Vacancy.score), desc(Vacancy.first_seen)).limit(min(max(limit, 1), 500))
    if bucket in {"qualified", "review", "rejected"}:
        stmt = stmt.where(Vacancy.bucket == bucket)
    elif bucket == "all":
        pass
    else:
        raise HTTPException(400, "Неизвестная категория")
    rows = db.scalars(stmt).all()
    qn = q.lower().strip()
    if qn:
        rows = [r for r in rows if qn in (r.title + " " + r.employer + " " + r.description).lower()]
    return [{"id": r.id, "source": r.source, "title": r.title, "employer": r.employer, "city": r.city,
             "description": r.description[:1400], "salary_min": r.salary_min, "salary_max": r.salary_max,
             "currency": r.currency, "salary_type": r.salary_type, "net_min_estimate": r.net_min_estimate,
             "schedule": r.schedule, "experience": r.experience, "published_at": r.published_at,
             "url": r.url, "score": r.score, "score_reasons": r.score_reasons.splitlines(),
             "bucket": r.bucket, "review_reason": r.review_reason, "cover_letter": r.cover_letter,
             "user_status": r.user_status, "first_seen": r.first_seen.isoformat() if r.first_seen else None} for r in rows]


@app.post("/api/search/run")
async def search_now(_: bool = Depends(basic_auth)):
    try:
        return await run_search()
    except Exception as exc:
        raise HTTPException(502, f"Ошибка поиска: {type(exc).__name__}: {exc}")


@app.post("/api/vacancies/{vacancy_id}/status")
async def update_vacancy_status(vacancy_id: int, payload: dict[str, str], db: Session = Depends(get_db), _: bool = Depends(basic_auth)):
    allowed = {"new", "saved", "rejected", "drafted", "applied-manually", "interview"}
    status = payload.get("status", "")
    if status not in allowed:
        raise HTTPException(400, "Недопустимый статус")
    job = db.get(Vacancy, vacancy_id)
    if not job:
        raise HTTPException(404, "Вакансия не найдена")
    job.user_status = status
    db.commit()
    return {"ok": True, "status": status}


@app.post("/api/vacancies/{vacancy_id}/letter")
async def regenerate_letter(vacancy_id: int, db: Session = Depends(get_db), _: bool = Depends(basic_auth)):
    job = db.get(Vacancy, vacancy_id)
    if not job:
        raise HTTPException(404, "Вакансия не найдена")
    vacancy_dict = {"title": job.title, "employer": job.employer, "description": job.description}
    letter = build_cover_letter(vacancy_dict)
    if settings.llm_base_url and settings.llm_api_key and settings.llm_model:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                response = await client.post(
                    settings.llm_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json={"model": settings.llm_model, "temperature": 0.3, "messages": [
                        {"role": "system", "content": "Ты пишешь краткие персонализированные сопроводительные письма на русском. Не выдумывай навыки и не добавляй 1С:ЗУП. Опыт кандидата: 4 года расчёта зарплаты в бюджетной централизованной бухгалтерии образовательных учреждений, около 300 лицевых счетов, 1С:БГУ, Excel, отпускные, больничные, табели и отчётность."},
                        {"role": "user", "content": f"Вакансия: {job.title}\nРаботодатель: {job.employer}\nОписание: {job.description[:5000]}\nСоставь письмо до 1200 знаков."}
                    ]}
                )
                response.raise_for_status()
                letter = response.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            log.warning("LLM generation failed; using template: %s", exc)
    job.cover_letter = letter
    job.user_status = "drafted"
    db.commit()
    return {"letter": letter, "ai_mode": bool(settings.llm_base_url and settings.llm_api_key and settings.llm_model)}


@app.get("/api/runs")
async def runs(db: Session = Depends(get_db), _: bool = Depends(basic_auth)):
    rows = db.scalars(select(SearchRun).order_by(desc(SearchRun.id)).limit(20)).all()
    return [{"id": r.id, "started_at": r.started_at.isoformat() if r.started_at else None,
             "finished_at": r.finished_at.isoformat() if r.finished_at else None,
             "received": r.received, "inserted": r.inserted, "qualified": r.qualified,
             "review": r.review, "error": r.error} for r in rows]


@app.get("/api/export.csv")
async def export_csv(db: Session = Depends(get_db), _: bool = Depends(basic_auth)):
    rows = db.scalars(select(Vacancy).order_by(desc(Vacancy.first_seen))).all()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Источник", "Должность", "Работодатель", "Город", "Зарплата от", "Зарплата до", "Тип", "Оценка NET", "Соответствие", "Категория", "Ссылка", "Причина проверки"])
    for r in rows:
        writer.writerow([r.source, r.title, r.employer, r.city, r.salary_min, r.salary_max, r.salary_type, r.net_min_estimate, r.score, r.bucket, r.url, r.review_reason])
    data = "\ufeff" + output.getvalue()
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=vacancies.csv"})
