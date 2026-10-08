from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from scout.bot import TradingBot
from scout.models import Report
from scout.ranking import rank


@pytest.fixture
def trading_bot(settings, now):
    settings.telegram_bot_token = "123:test"
    settings.telegram_owner_id = 123
    bot = TradingBot(settings)
    bot.service.now = lambda: now
    bot.refreshed_day = now.date().isoformat()
    return bot


def seed(bot, idea, quote, verdict, settings, now):
    item, _ = rank(idea, verdict, [quote], settings, now, [])
    report = Report(
        day=now.date().isoformat(), generated_at=now, market_context="", ideas=[item], rejected=[], notes=[]
    )
    bot.storage.save_report(report)
    return report


async def test_delivered_once_with_persistent_history(trading_bot, idea, quote, verdict, settings, now):
    report = seed(trading_bot, idea, quote, verdict, settings, now)
    transport = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1234)))
    await trading_bot.publish(transport)
    await trading_bot.publish(transport)
    assert transport.send_message.await_count == 1
    assert trading_bot.storage.delivery(report.day) == "sent"
    assert len(trading_bot.storage.history(now, 90)) == 1


async def test_timeout_does_not_blindly_resend(trading_bot, idea, quote, verdict, settings, now):
    report = seed(trading_bot, idea, quote, verdict, settings, now)
    transport = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
    await trading_bot.publish(transport)
    await trading_bot.publish(transport)
    assert transport.send_message.await_count == 1
    assert trading_bot.storage.delivery(report.day) == "uncertain"
    assert not trading_bot.storage.history(now, 90)


async def test_missing_report_sends_delay_notice_once(trading_bot):
    transport = SimpleNamespace(send_message=AsyncMock())
    await trading_bot.publish(transport)
    await trading_bot.publish(transport)
    assert transport.send_message.await_count == 1
    assert "ещё не готов" in transport.send_message.call_args.args[1]


def test_private_access_requires_owner(trading_bot):
    def update(user, chat):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(id=chat)
        )

    assert trading_bot.authorized(update(123, 123))
    assert not trading_bot.authorized(update(456, 456))
    assert not trading_bot.authorized(update(123, -100999))


async def test_old_report_never_used_for_today(trading_bot, now):
    old = Report(day="2026-10-06", generated_at=now, market_context="", ideas=[], rejected=[], notes=[])
    trading_bot.storage.save_report(old)
    transport = SimpleNamespace(send_message=AsyncMock())
    await trading_bot.publish(transport)
    assert "ещё не готов" in transport.send_message.call_args.args[1]
