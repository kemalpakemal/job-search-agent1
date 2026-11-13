import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import telegram_bot


class TelegramBotTests(unittest.TestCase):
    def make_job(self):
        return SimpleNamespace(
            id=123,
            title="Бухгалтер по расчёту заработной платы",
            employer="ООО Пример",
            city="Сочи",
            source="HeadHunter",
            salary_min=70000,
            salary_max=90000,
            salary_type="gross",
            net_min_estimate=60900,
            schedule="Полный день, 5/2",
            experience="От 1 года до 3 лет",
            score=92,
            score_reasons="Опыт расчёта зарплаты\n1С:БГУ\nБюджетная организация",
            review_reason="",
            description="Расчёт зарплаты, отпускных и больничных.",
            url="https://example.com/vacancy/123",
        )

    def test_card_contains_pay_and_score(self):
        text = telegram_bot._job_text(self.make_job())
        self.assertIn("Оценка на руки", text)
        self.assertIn("92/100", text)
        self.assertIn("Сочи", text)

    def test_keyboard_has_manual_actions_only(self):
        markup = telegram_bot._job_keyboard(self.make_job(), "qualified")
        callbacks = [button.get("callback_data") for row in markup["inline_keyboard"] for button in row]
        self.assertIn("save:123", callbacks)
        self.assertIn("skip:123", callbacks)
        self.assertIn("letter:123", callbacks)
        self.assertIn("applied:123", callbacks)
        self.assertTrue(all(not str(x).startswith("auto") for x in callbacks))
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://example.com/vacancy/123")

    def test_user_id_allowlist(self):
        with patch.object(telegram_bot, "_allowed_ids", return_value={123456, 234567}):
            self.assertTrue(telegram_bot._is_allowed(123456))
            self.assertFalse(telegram_bot._is_allowed(999999))
            self.assertFalse(telegram_bot._is_allowed(None))


class TelegramBackgroundSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_remains_responsive_during_search(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_search(notify=False):
            started.set()
            await release.wait()
            return {"received": 4, "inserted": 2, "qualified": 0, "review": 2, "sources": {}}

        search_update = {
            "message": {
                "chat": {"id": 1501875166, "type": "private"},
                "from": {"id": 1501875166},
                "text": "🔎 Найти вакансии",
            }
        }
        sources_update = {
            "message": {
                "chat": {"id": 1501875166, "type": "private"},
                "from": {"id": 1501875166},
                "text": "🔌 Источники",
            }
        }

        old_fn = telegram_bot._search_fn
        old_task = telegram_bot._search_task
        telegram_bot._search_fn = slow_search
        telegram_bot._search_task = None
        send_mock = AsyncMock()
        try:
            with patch.object(telegram_bot, "_is_allowed", return_value=True), \
                 patch.object(telegram_bot, "_remember_chat"), \
                 patch.object(telegram_bot, "_send", send_mock):
                await asyncio.wait_for(telegram_bot._handle_message(search_update), timeout=0.5)
                await asyncio.wait_for(started.wait(), timeout=0.5)

                # A second Telegram update must be processed before the slow search ends.
                await asyncio.wait_for(telegram_bot._handle_message(sources_update), timeout=0.5)
                self.assertTrue(any("Источники вакансий" in str(call.args[1]) for call in send_mock.await_args_list))

                release.set()
                task = telegram_bot._search_task
                if task is not None:
                    await asyncio.wait_for(task, timeout=1.0)
                self.assertTrue(any("Поиск завершён" in str(call.args[1]) for call in send_mock.await_args_list))
        finally:
            release.set()
            telegram_bot._search_task = old_task
            telegram_bot._search_fn = old_fn



if __name__ == "__main__":
    unittest.main()
