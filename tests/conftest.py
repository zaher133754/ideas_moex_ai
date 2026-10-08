from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scout.config import Settings
from scout.models import Idea, Quote, Source, Verdict


@pytest.fixture
def now():
    return datetime(2026, 10, 7, 9, tzinfo=ZoneInfo("Europe/Samara"))


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


@pytest.fixture
def idea(now):
    return Idea(
        id="test",
        title="Тестовая идея для проверки фильтров",
        kind="stock",
        setup="event",
        catalyst_key="SBER report 2026-10-08",
        catalyst_at=now + timedelta(days=1),
        discovered_at=now,
        valid_until=now + timedelta(days=2),
        horizon="1–5 дней",
        thesis="Тестовое обоснование идеи с конкретным предстоящим событием и проверяемым условием входа.",
        entry_condition="Вход только при удержании уровня поддержки",
        invalidation="Отмена при закрытии ниже стопа",
        risks=["Риск гэпа на событии", "Риск изменения рыночных условий"],
        prerequisites=[],
        leverage=1,
        margin_and_borrow="Без заёмных средств и дополнительного обеспечения",
        legs=[
            dict(
                ticker="SBER",
                board="TQBR",
                direction="buy",
                unit="RUB",
                ratio=1,
                entry_low=100,
                entry_high=101,
                stop=98,
                targets=[110, 115],
            )
        ],
        sources=[
            Source(
                url="https://issuer.example/news/report",
                title="Публикация о событии",
                publisher="Эмитент",
                published_at=now - timedelta(hours=2),
                checked_at=now,
                date_evidence="Дата и время публикации указаны на странице",
                supports="Источник подтверждает предстоящее событие",
                primary=True,
            )
        ],
        bond=None,
        arbitrage=None,
    )


@pytest.fixture
def quote(now):
    return Quote(
        ticker="SBER",
        board="TQBR",
        name="Сбербанк",
        price=100.5,
        price_at=now - timedelta(minutes=16),
        fetched_at=now,
        daily_turnover_rub=50_000_000,
        lot_size=1,
        face_value=3,
        accrued_interest=None,
        yield_pct=None,
        bid=100.49,
        ask=100.51,
        currency="RUB",
        book_at=now - timedelta(minutes=15),
        trading_status="T",
        rub_per_price_unit_per_lot=1,
        initial_margin_rub=None,
        duration_days=None,
        maturity=None,
        expiry=None,
        source_url="https://iss.moex.com/test",
        candles=[],
    )


@pytest.fixture
def verdict():
    return Verdict(
        idea_id="test",
        approved=True,
        evidence=9,
        thesis=9,
        risk=9,
        feasibility=9,
        source_dates_verified=True,
        levels_supported=True,
        reasoning="Источники и параметры подтверждают логику идеи, уровни согласованы с ценой.",
        weaknesses=["Стоп не защищает от ценового гэпа"],
    )
