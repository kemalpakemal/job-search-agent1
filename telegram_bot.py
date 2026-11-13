from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import desc, select, func, or_

from config import settings
from database import AppSetting, SessionLocal, Vacancy, SearchRun

log = logging.getLogger("job-agent.telegram")
SearchCallable = Callable[..., Awaitable[dict[str, Any]]]

_poll_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_client: httpx.AsyncClient | None = None
_search_fn: SearchCallable | None = None
_search_lock = asyncio.Lock()
_search_task: asyncio.Task | None = None
_offset = 0

MAIN_MENU = {
    "keyboard": [
        [{"text": "🔎 Найти вакансии"}, {"text": "📋 Подходящие"}],
        [{"text": "🕵 Проверить"}, {"text": "⭐ Сохранённые"}],
        [{"text": "📊 Статистика"}, {"text": "⚙️ Настройки"}],
        [{"text": "🔌 Источники"}, {"text": "❔ Помощь"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
    "input_field_placeholder": "Выбери действие",
}


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def _allowed_ids() -> set[int]:
    values: set[int] = set()
    for part in settings.telegram_allowed_user_ids.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            values.add(int(part))
    chat_id = settings.telegram_chat_id.strip()
    if chat_id.lstrip("-").isdigit():
        values.add(int(chat_id))
    return values


def _is_allowed(user_id: int | None) -> bool:
    return bool(user_id is not None and user_id in _allowed_ids())


def _remember_chat(chat_id: int, chat_type: str) -> None:
    if chat_type != "private":
        return
    db = SessionLocal()
    try:
        row = db.get(AppSetting, "telegram_chat_id")
        if row is None:
            db.add(AppSetting(key="telegram_chat_id", value=str(chat_id)))
        else:
            row.value = str(chat_id)
        db.commit()
    finally:
        db.close()


async def _call(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if _client is None:
        raise RuntimeError("Telegram client is not initialized")
    response = await _client.post(_api_url(method), json=payload or {})
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {data.get('description', 'unknown error')}")
    return data.get("result") or {}


async def _send(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None, disable_preview: bool = True) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await _call("sendMessage", payload)


async def _answer_callback(callback_id: str, text: str = "", alert: bool = False) -> None:
    try:
        await _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180], "show_alert": alert})
    except Exception as exc:
        log.warning("Could not answer Telegram callback (%s)", type(exc).__name__)


def _count(db, bucket: str | None = None, status: str | None = None) -> int:
    stmt = select(func.count(Vacancy.id))
    if bucket:
        stmt = stmt.where(Vacancy.bucket == bucket)
    if status:
        stmt = stmt.where(Vacancy.user_status == status)
    return int(db.scalar(stmt) or 0)


def _salary_text(job: Vacancy) -> str:
    def money(value: float) -> str:
        return f"{round(value):,}".replace(",", " ") + " ₽"
    if job.salary_min is None:
        original = "не указана"
    else:
        original = money(job.salary_min)
        if job.salary_max is not None:
            original += f" – {money(job.salary_max)}"
        original += " (" + ("до НДФЛ" if job.salary_type == "gross" else "на руки" if job.salary_type == "net" else "тип не уточнён") + ")"
    net = f"Оценка на руки: от {money(job.net_min_estimate)}" if job.net_min_estimate is not None else "Оценка на руки: не определена"
    return f"Зарплата: {original}\n{net}"


def _job_text(job: Vacancy, review: bool = False) -> str:
    text = (
        f"{job.title}\n{job.employer}\n"
        f"📍 {job.city or 'Город не указан'} · {job.source}\n"
        f"💰 {_salary_text(job)}\n"
        f"📅 График: {job.schedule or 'не указан'}\n"
        f"Опыт: {job.experience or 'не указан'}\n"
        f"🎯 Соответствие: {job.score}/100"
    )
    reasons = [x for x in (job.score_reasons or "").splitlines() if x]
    if reasons:
        text += "\n\nПочему подходит:\n• " + "\n• ".join(reasons[:3])
    if review and job.review_reason:
        text += "\n\n⚠️ Требует проверки:\n" + job.review_reason
    if job.description:
        desc = " ".join(job.description.split())[:650]
        if desc:
            text += "\n\nОписание:\n" + desc
    return text[:3900]


def _job_keyboard(job: Vacancy, bucket: str) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if job.url.startswith(("https://", "http://")):
        rows.append([{"text": "Открыть вакансию", "url": job.url}])
    rows.append([{"text": "📄 Сопроводительное письмо", "callback_data": f"letter:{job.id}"}])
    rows.append([
        {"text": "⭐ Сохранить", "callback_data": f"save:{job.id}"},
        {"text": "Пропустить", "callback_data": f"skip:{job.id}"},
    ])
    rows.append([{"text": "✅ Уже откликнулся", "callback_data": f"applied:{job.id}"}])
    rows.append([{"text": "Следующая ➡️", "callback_data": f"next:{bucket}:{job.id}"}])
    return {"inline_keyboard": rows}


def _get_next_job(db, bucket: str, before_id: int | None = None) -> Vacancy | None:
    stmt = select(Vacancy).order_by(desc(Vacancy.id))
    if bucket == "saved":
        stmt = stmt.where(Vacancy.user_status == "saved")
    elif bucket == "review":
        # Keep the review queue visible even if older records have a non-standard
        # status value. Hide only explicit user decisions.
        stmt = stmt.where(
            Vacancy.bucket == "review",
            or_(
                Vacancy.user_status.is_(None),
                ~Vacancy.user_status.in_(["rejected", "saved", "applied-manually", "interview"]),
            ),
        )
    else:
        stmt = stmt.where(Vacancy.bucket == bucket, Vacancy.user_status.in_(["new", "drafted"]))
    if before_id is not None:
        stmt = stmt.where(Vacancy.id < before_id)
    return db.scalars(stmt.limit(1)).first()


async def _show_next(chat_id: int, bucket: str, before_id: int | None = None) -> None:
    db = SessionLocal()
    try:
        job = _get_next_job(db, bucket, before_id)
        if not job:
            if before_id is not None:
                message = "Это последняя вакансия в списке."
            elif bucket == "review":
                all_review = _count(db, bucket="review")
                excluded = _count(db, bucket="review", status="rejected") + _count(db, bucket="review", status="saved") + _count(db, bucket="review", status="applied-manually") + _count(db, bucket="review", status="interview")
                message = (
                    "Очередь проверки сейчас пуста.\n"
                    f"Всего вакансий в категории: {all_review}\n"
                    f"Скрыто по ранее выбранным статусам: {excluded}\n"
                    "Сохранённые вакансии доступны в разделе «Сохранённые»."
                )
            else:
                message = "В этой категории пока нет вакансий."
            await _send(chat_id, message, MAIN_MENU)
            return
        await _send(chat_id, _job_text(job, review=bucket == "review"), _job_keyboard(job, bucket))
    finally:
        db.close()


async def _cmd_id(chat_id: int, user_id: int | None) -> None:
    await _send(chat_id, f"Твой Telegram ID: {user_id if user_id is not None else 'не определён'}\nЭто не пароль и не код входа. Используй номер только для настройки доступа к своему боту.")


async def _deny(chat_id: int, user_id: int | None) -> None:
    await _send(
        chat_id,
        "Бот запущен, но доступ ещё не закреплён за владельцем.\n\n"
        f"Твой Telegram ID: {user_id if user_id is not None else 'не определён'}\n\n"
        "Добавь этот номер в переменную TELEGRAM_ALLOWED_USER_IDS в настройках сервера, затем перезапусти приложение.\n"
        "Для личных уведомлений тот же номер можно указать в TELEGRAM_CHAT_ID.\n"
        "Пароль Telegram и коды входа никому не отправляй."
    )


async def _handle_message(update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user = message.get("from") or {}
    user_id = user.get("id")
    chat_type = chat.get("type", "")
    text = (message.get("text") or "").strip()
    if not chat_id:
        return
    if text.startswith("/id"):
        await _cmd_id(int(chat_id), int(user_id) if user_id is not None else None)
        return
    if not _is_allowed(int(user_id) if user_id is not None else None):
        await _deny(int(chat_id), int(user_id) if user_id is not None else None)
        return
    _remember_chat(int(chat_id), chat_type)

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if command in {"/start", "/help", ""} and (command != "" or text in {"❔ Помощь"}):
        if command == "/start":
            await _send(
                int(chat_id),
                "Агент поиска работы запущен.\n\n"
                f"Город: {settings.target_city}\nМинимум: {settings.min_net_salary:,} ₽ на руки\n"
                "График: 5/2\nПорог соответствия: 80/100\n\n"
                "Отклики автоматически не отправляются. Ты просматриваешь вакансии и сам решаешь, что делать.",
                MAIN_MENU,
            )
        else:
            await _send(int(chat_id), "Команды:\n/search — поиск сейчас\n/jobs — подходящие\n/review — проверить\n/saved — сохранённые\n/stats — статистика\n/sources — источники\n/settings — параметры\n/id — твой ID", MAIN_MENU)
        return

    if command in {"/search"} or text == "🔎 Найти вакансии":
        global _search_task
        if _search_fn is None:
            await _send(int(chat_id), "Функция поиска не инициализирована. Проверь журнал сервера.")
            return
        if _search_lock.locked() or (_search_task is not None and not _search_task.done()):
            await _send(int(chat_id), "Поиск уже выполняется. Я сообщу результат отдельным сообщением.")
            return
        await _send(int(chat_id), "Поиск запущен в фоне. Бот продолжит отвечать на кнопки и команды; результат пришлю отдельным сообщением.", MAIN_MENU)
        _search_task = asyncio.create_task(_run_search_for_chat(int(chat_id)), name=f"telegram-search-{chat_id}")
        return

    if command in {"/jobs"} or text == "📋 Подходящие":
        await _show_next(int(chat_id), "qualified")
        return
    if command in {"/review"} or text == "🕵 Проверить":
        await _show_next(int(chat_id), "review")
        return
    if command in {"/saved"} or text == "⭐ Сохранённые":
        await _show_next(int(chat_id), "saved")
        return
    if command in {"/stats"} or text == "📊 Статистика":
        db = SessionLocal()
        try:
            total = _count(db)
            qualified = _count(db, bucket="qualified")
            review = _count(db, bucket="review")
            saved = _count(db, status="saved")
            latest = db.scalars(select(SearchRun).order_by(desc(SearchRun.id)).limit(1)).first()
            last = latest.finished_at.strftime("%d.%m.%Y %H:%M UTC") if latest and latest.finished_at else "ещё не запускался"
            await _send(int(chat_id), f"Статистика агента\n\nВсего вакансий: {total}\nПодходят: {qualified}\nНужна проверка: {review}\nСохранено: {saved}\nПоследний поиск: {last}\nАвтоотклики: выключены", MAIN_MENU)
        finally:
            db.close()
        return
    if command in {"/sources"} or text == "🔌 Источники":
        db = SessionLocal()
        try:
            latest = db.scalars(select(SearchRun).order_by(desc(SearchRun.id)).limit(1)).first()
            statuses = json.loads(latest.source_status) if latest and latest.source_status else {}
            if not statuses:
                statuses = {
                    "HeadHunter": "Будет проверен при первом поиске",
                    "Работа России": "Будет проверен при первом поиске",
                    "SuperJob": "Нужен API-ключ приложения",
                    "Зарплата.ру": "Ручной поиск",
                    "Работа.ру": "Ручной поиск",
                    "Авито Работа": "Ручной поиск",
                }
            body = "Источники вакансий:\n\n" + "\n\n".join(f"{name}: {status}" for name, status in statuses.items())
            await _send(int(chat_id), body[:3900], MAIN_MENU)
        finally:
            db.close()
        return
    if command in {"/settings"} or text == "⚙️ Настройки":
        await _send(
            int(chat_id),
            "Текущие параметры поиска:\n\n"
            f"Город: {settings.target_city}\nМинимум: {settings.min_net_salary:,} ₽ на руки\n"
            "График: 5/2\nПорог соответствия: 80/100\n"
            f"Интервал поиска: {settings.search_interval_hours} ч.\nОтклики: только после ручного подтверждения.",
            MAIN_MENU,
        )
        return
    if command == "/help" or text == "❔ Помощь":
        await _send(int(chat_id), "Команды: /search /jobs /review /saved /stats /sources /settings /id", MAIN_MENU)
        return


async def _run_search_for_chat(chat_id: int) -> None:
    """Run manual search in a background task without blocking Telegram polling."""
    global _search_task
    try:
        async with _search_lock:
            search_fn = _search_fn
            if search_fn is None:
                await _send(chat_id, "Функция поиска не инициализирована. Проверь журнал сервера.", MAIN_MENU)
                return
            started = asyncio.get_running_loop().time()
            result = await search_fn(notify=False)
            elapsed = int(asyncio.get_running_loop().time() - started)
            await _send(
                chat_id,
                "Поиск завершён.\n"
                f"Получено: {result.get('received', 0)}\nНовых: {result.get('inserted', 0)}\n"
                f"Подходят: {result.get('qualified', 0)}\nНужна проверка: {result.get('review', 0)}\n"
                f"Время: {elapsed} сек.\n\nОтклики не отправлялись.",
                MAIN_MENU,
            )
            if result.get("qualified", 0):
                await _show_next(chat_id, "qualified")
    except asyncio.CancelledError:
        log.info("Manual Telegram search cancelled during shutdown")
        raise
    except Exception as exc:
        log.exception("Manual Telegram search failed")
        try:
            await _send(chat_id, f"Не удалось завершить поиск: {type(exc).__name__}: {str(exc)[:400]}", MAIN_MENU)
        except Exception as exc:
            log.warning("Could not send search failure message to Telegram (%s)", type(exc).__name__)
    finally:
        # Clear only our own task reference; do not erase a newer search task.
        current = asyncio.current_task()
        if _search_task is current:
            _search_task = None


async def _handle_callback(update: dict[str, Any]) -> None:
    callback = update.get("callback_query") or {}
    callback_id = callback.get("id")
    user = callback.get("from") or {}
    user_id = user.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not callback_id:
        return
    if not _is_allowed(int(user_id) if user_id is not None else None):
        await _answer_callback(callback_id, "Нет доступа. Настрой TELEGRAM_ALLOWED_USER_IDS.", True)
        return
    if not chat_id:
        await _answer_callback(callback_id, "Не удалось определить чат", True)
        return
    data = callback.get("data", "")
    try:
        parts = data.split(":")
        action = parts[0]
        if action == "next" and len(parts) == 3:
            await _answer_callback(callback_id)
            await _show_next(int(chat_id), parts[1], int(parts[2]))
            return
        if action in {"save", "skip", "letter", "applied", "interview"} and len(parts) == 2:
            job_id = int(parts[1])
            db = SessionLocal()
            try:
                job = db.get(Vacancy, job_id)
                if not job:
                    await _answer_callback(callback_id, "Вакансия не найдена", True)
                    return
                if action == "save":
                    job.user_status = "saved"
                    db.commit()
                    await _answer_callback(callback_id, "Вакансия сохранена")
                    await _send(int(chat_id), "Добавил вакансию в сохранённые.", MAIN_MENU)
                elif action == "applied":
                    job.user_status = "applied-manually"
                    db.commit()
                    await _answer_callback(callback_id, "Отметил как отправленный отклик")
                    await _send(int(chat_id), "Отметил вакансию как «Отклик отправлен». Автоматически ничего не отправлялось.", MAIN_MENU)
                elif action == "interview":
                    job.user_status = "interview"
                    db.commit()
                    await _answer_callback(callback_id, "Отмечено приглашение")
                    await _send(int(chat_id), "Вакансия отмечена как «Приглашение на собеседование».", MAIN_MENU)
                elif action == "skip":
                    job.user_status = "rejected"
                    db.commit()
                    await _answer_callback(callback_id, "Вакансия скрыта")
                    await _show_next(int(chat_id), job.bucket, job.id)
                else:
                    letter = job.cover_letter or "Сопроводительное письмо пока не сформировано."
                    await _answer_callback(callback_id, "Письмо готово")
                    keyboard = {"inline_keyboard": [[{"text": "Открыть вакансию", "url": job.url}]]} if job.url.startswith(("https://", "http://")) else None
                    await _send(
                        int(chat_id),
                        "Сопроводительное письмо:\n\n" + letter[:3500] + "\n\nНажми и удерживай текст, чтобы скопировать его. Перед отправкой проверь письмо.",
                        keyboard,
                    )
            finally:
                db.close()
    except Exception as exc:
        log.warning("Telegram callback failed (%s)", type(exc).__name__)
        await _answer_callback(callback_id, f"Ошибка обработки действия: {type(exc).__name__}", True)


async def _poll() -> None:
    global _offset
    assert _client is not None and _stop_event is not None
    while not _stop_event.is_set():
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message", "callback_query"])}
            if _offset:
                params["offset"] = _offset
            response = await _client.get(_api_url("getUpdates"), params=params, timeout=60)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "Telegram getUpdates failed"))
            for update in data.get("result", []):
                _offset = int(update.get("update_id", 0)) + 1
                try:
                    if "message" in update or "edited_message" in update:
                        await _handle_message(update)
                    elif "callback_query" in update:
                        await _handle_callback(update)
                except Exception as exc:
                    log.warning("Failed to process Telegram update %s (%s)", update.get("update_id"), type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 409:
                log.error("Telegram polling conflict (HTTP 409): another long-polling process may be active; retrying in 10 seconds")
                await asyncio.sleep(10)
            else:
                log.error("Telegram polling failed (HTTP %s); retrying in 5 seconds", status)
                await asyncio.sleep(5)
        except Exception as exc:
            log.error("Telegram polling error (%s); retrying in 5 seconds", type(exc).__name__)
            await asyncio.sleep(5)


async def start_telegram_bot(search_fn: SearchCallable) -> asyncio.Task | None:
    global _poll_task, _stop_event, _client, _search_fn, _offset
    token = settings.telegram_bot_token.strip()
    if not token:
        log.info("Telegram bot disabled: TELEGRAM_BOT_TOKEN not set")
        return None
    _search_fn = search_fn
    _offset = 0
    _stop_event = asyncio.Event()
    _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
    # Long polling and webhooks are mutually exclusive in Telegram. This bot uses polling.
    try:
        await _call("deleteWebhook", {"drop_pending_updates": False})
        me = await _call("getMe")
        await _call("setMyCommands", {"commands": [
            {"command": "start", "description": "Открыть меню"},
            {"command": "search", "description": "Найти новые вакансии"},
            {"command": "jobs", "description": "Подходящие вакансии"},
            {"command": "review", "description": "Вакансии на проверку"},
            {"command": "saved", "description": "Сохранённые вакансии"},
            {"command": "stats", "description": "Статистика поиска"},
            {"command": "sources", "description": "Состояние источников"},
            {"command": "settings", "description": "Параметры поиска"},
            {"command": "id", "description": "Показать мой Telegram ID"},
        ]})
        log.info("Telegram bot authorized as @%s", me.get("username", "unknown"))
        _poll_task = asyncio.create_task(_poll(), name="telegram-long-polling")
        log.info("Telegram long polling started")
        return _poll_task
    except Exception as exc:
        await _client.aclose()
        _client = None
        _stop_event = None
        log.error("Telegram bot could not start (%s)", type(exc).__name__)
        return None


async def stop_telegram_bot() -> None:
    global _poll_task, _search_task, _stop_event, _client, _search_fn
    if _stop_event:
        _stop_event.set()
    if _search_task:
        _search_task.cancel()
        try:
            await _search_task
        except asyncio.CancelledError:
            pass
    _search_task = None
    if _poll_task:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None
    _stop_event = None
    _search_fn = None
    if _client:
        await _client.aclose()
    _client = None
    log.info("Telegram long polling stopped")
