import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('TELEGRAM_TOKEN', '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk')
os.environ.setdefault('DARKAPI_KEY', 'test-key')
import bot
from runtime import acquire_polling_lock, reply_models
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.methods import GetUpdates


class RuntimeTests(unittest.TestCase):
    def test_only_catalog_models_and_three_attempts(self):
        self.assertEqual(reply_models('chat', '', ['chat', 'fallback', 'other', 'fourth'],
                                     ['nonexistent', 'fallback'], set()),
                         ['chat', 'fallback', 'other'])

    def test_rejected_model_is_skipped(self):
        self.assertEqual(reply_models('bad', '', ['bad', 'good'], [], {'bad'}), ['good'])

    def test_duplicate_process_lock(self):
        lock = acquire_polling_lock('unit-test-token')
        try:
            with self.assertRaises(RuntimeError):
                acquire_polling_lock('unit-test-token')
        finally:
            lock.close()
        acquire_polling_lock('unit-test-token').close()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot.unsupported_models.clear()

    async def test_conflict_and_bad_token_are_not_retried(self):
        for kind in (TelegramConflictError, TelegramUnauthorizedError):
            fake = AsyncMock(side_effect=kind(method=GetUpdates(), message='test'))
            updates = bot.SinglePollerDispatcher._listen_updates(fake)
            with self.assertRaises(kind):
                await anext(updates)
            self.assertEqual(fake.await_count, 1)

    async def test_polling_offset(self):
        fake = AsyncMock(return_value=[SimpleNamespace(update_id=8)])
        updates = bot.SinglePollerDispatcher._listen_updates(fake)
        await anext(updates)
        await anext(updates)
        self.assertEqual(fake.call_args.args[0].offset, 9)
        await updates.aclose()

    async def test_unsupported_is_not_sent_again(self):
        create = AsyncMock(side_effect=[RuntimeError('unsupported_model'),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Привет'))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Привет'))])])
        with patch.object(bot, 'AVAILABLE_MODELS', ['bad', 'good']), \
             patch.object(bot, 'CHAT_MODEL', 'bad'), \
             patch.object(bot.ai_client.chat.completions, 'create', create):
            self.assertEqual(await bot.generate_phil_reply(1, 'тест'), 'Привет')
            self.assertEqual(await bot.generate_phil_reply(1, 'тест'), 'Привет')
        self.assertEqual([c.kwargs['model'] for c in create.call_args_list], ['bad', 'good', 'good'])

    async def test_image_failure_does_not_recurse(self):
        create = AsyncMock(side_effect=RuntimeError('failure'))
        with patch.object(bot, 'AVAILABLE_MODELS', ['a', 'b', 'c', 'd']), \
             patch.object(bot, 'CHAT_MODEL', 'a'), \
             patch.object(bot, 'VISION_MODEL', 'a'), \
             patch.object(bot.ai_client.chat.completions, 'create', create):
            self.assertEqual(await bot.generate_phil_reply(1, 'тест', image_b64='test'), '')
        self.assertEqual(create.await_count, 3)

    async def test_penalty_retries_same_model(self):
        create = AsyncMock(side_effect=[RuntimeError('frequency_penalty unsupported'),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Ок'))])])
        with patch.object(bot, 'PENALTY_OK', True), \
             patch.object(bot, 'AVAILABLE_MODELS', ['a']), \
             patch.object(bot, 'CHAT_MODEL', 'a'), \
             patch.object(bot.ai_client.chat.completions, 'create', create):
            self.assertEqual(await bot.generate_phil_reply(1, 'тест'), 'Ок')
        self.assertEqual([c.kwargs['model'] for c in create.call_args_list], ['a', 'a'])
        self.assertNotIn('frequency_penalty', create.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
