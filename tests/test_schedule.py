import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scout.bot import TradingBot
from scout.models import Report


@pytest.mark.parametrize("minute_delta,expected_messages", [(-1, 0), (0, 1), (10, 1), (181, 0)])
async def test_delivery_window_in_samara_even_when_server_uses_other_timezone(
    settings, now, monkeypatch, minute_delta, expected_messages
):
    settings.telegram_bot_token, settings.telegram_owner_id = "123:test", 123
    bot = TradingBot(settings)
    current = now + timedelta(minutes=minute_delta)
    bot.service.now = lambda: current
    day = current.date().isoformat()
    bot.refreshed_day = day
    bot.storage.save_report(
        Report(day=day, generated_at=current, market_context="", ideas=[], rejected=[], notes=[])
    )
    transport = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))

    async def stop_after_tick(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)
    with pytest.raises(asyncio.CancelledError):
        await bot.scheduler(transport)
    await asyncio.gather(*list(bot.background))
    assert transport.send_message.await_count == expected_messages


async def test_midnight_report_does_not_replace_morning_research(settings, now, monkeypatch):
    settings.telegram_bot_token, settings.telegram_owner_id = "123:test", 123
    bot = TradingBot(settings)
    bot.service.now = lambda: now
    midnight = now.replace(hour=0)
    day = now.date().isoformat()
    report = Report(day=day, generated_at=midnight, market_context="", ideas=[], rejected=[], notes=[])
    bot.storage.save_report(report)
    bot.storage.job_state(day, "ready", midnight)
    bot.service.research = AsyncMock(return_value=report)
    transport = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))

    async def stop_after_tick(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_tick)
    with pytest.raises(asyncio.CancelledError):
        await bot.scheduler(transport)
    await asyncio.gather(*list(bot.background))
    assert bot.service.research.await_count == 1
    assert "Утренний отчёт ещё не готов" in transport.send_message.call_args.args[1]


async def test_scheduler_recovers_after_transient_failure(settings, monkeypatch):
    settings.telegram_bot_token, settings.telegram_owner_id = "123:test", 123
    bot = TradingBot(settings)
    bot._scheduler_loop = AsyncMock(
        side_effect=[RuntimeError("temporary storage failure"), asyncio.CancelledError()]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await bot.scheduler(SimpleNamespace())
    assert bot._scheduler_loop.await_count == 2
    sleep.assert_awaited_once_with(5)
