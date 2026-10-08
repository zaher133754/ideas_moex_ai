from datetime import timedelta

import pytest
from pydantic import ValidationError

from scout.models import ArbitrageDetails, BondDetails, Idea
from scout.ranking import canonical_url, eligibility, history_item, is_duplicate, rank


def test_good_idea_and_weight_total(idea, quote, verdict, settings, now):
    result, reason = rank(idea, verdict, [quote], settings, now, [])
    assert reason is None
    assert 80 < result.score <= 100
    assert result.rr > 2
    assert result.rr == pytest.approx((9 - (101 + 110) * 0.0005) / (3 + (101 + 98) * 0.0005))
    assert result.score == round(sum(result.components.values()), 1)


@pytest.mark.parametrize("hours,expected", [(47.99, None), (48.01, "Нет подтверждающего")])
def test_recency_boundary(idea, settings, now, hours, expected):
    idea.sources[0].published_at = now - timedelta(hours=hours)
    reason = eligibility(idea, settings, now, [])
    assert reason is None if expected is None else expected in reason


def test_old_primary_allowed_only_with_new_supporting_source(idea, settings, now):
    original = idea.sources[0].model_copy(update={"published_at": now - timedelta(days=100)})
    idea.sources.append(original)
    assert eligibility(idea, settings, now, []) is None
    idea.sources = [original]
    assert "Нет подтверждающего" in eligibility(idea, settings, now, [])


def test_future_source_rejected(idea, settings, now):
    idea.sources[0].published_at = now + timedelta(minutes=1)
    assert "будущим" in eligibility(idea, settings, now, [])


def test_event_already_happened(idea, settings, now):
    idea.catalyst_at = now - timedelta(seconds=1)
    assert "произошёл" in eligibility(idea, settings, now, [])


@pytest.mark.parametrize("price,fragment", [(98, "стоп"), (110, "далеко"), (120, "далеко")])
def test_chased_or_invalidated_entry(idea, quote, verdict, settings, now, price, fragment):
    quote.price = price
    result, reason = rank(idea, verdict, [quote], settings, now, [])
    assert result is None and fragment in reason


def test_target_hit_even_inside_allowed_distance(idea, quote, verdict, settings, now):
    idea.legs[0].entry_high = 108
    idea.legs[0].entry_low = 107
    quote.price = 110
    _, reason = rank(idea, verdict, [quote], settings, now, [])
    assert "достигнута" in reason


def test_fee_adjusted_rr(idea, quote, verdict, settings, now):
    settings.commission_bps = 100
    idea.legs[0].targets = [106]
    result, reason = rank(idea, verdict, [quote], settings, now, [])
    assert result is None and "отношение" in reason


def test_short_rr_and_borrow_condition(idea, quote, verdict, settings, now):
    leg = idea.legs[0]
    leg.direction, leg.stop, leg.targets = "sell", 103, [91]
    result, reason = rank(idea, verdict, [quote], settings, now, [])
    assert result is None and "заимствования" in reason
    idea.prerequisites = ["Брокер разрешает шорт; заём подтверждён перед сделкой"]
    result, reason = rank(idea, verdict, [quote], settings, now, [])
    assert reason is None and result.rr > 2


def test_quote_stale(idea, quote, verdict, settings, now):
    quote.price_at = now - timedelta(hours=97)
    result, reason = rank(idea, verdict, [quote], settings, now, [])
    assert result is None and "устарела" in reason


def test_unverified_source_dates_never_approved(idea, quote, verdict, settings, now):
    verdict.source_dates_verified = False
    result, reason = rank(idea, verdict, [quote], settings, now, [])
    assert result is None and "Критическая" in reason


def test_duplicate_despite_title_and_tracking_query(idea):
    history = [history_item(idea)]
    idea.title = "Полностью новый заголовок того же старого тезиса"
    idea.sources[0].url = "https://issuer.example/news/report?utm_source=telegram"
    assert is_duplicate(idea, history)
    assert canonical_url(str(idea.sources[0].url)) == "https://issuer.example/news/report"


def test_materially_new_catalyst_same_asset_allowed(idea):
    history = [history_item(idea)]
    idea.catalyst_key = "SBER dividend decision 2026-11-30"
    idea.sources[0].url = "https://issuer.example/news/new-dividend"
    assert not is_duplicate(idea, history)


def test_bond_price_units_enforced(idea):
    payload = idea.model_dump()
    payload["kind"] = "bond"
    payload["legs"][0]["board"] = "TQOB"
    payload["bond"] = BondDetails(
        yield_to_maturity_pct=16,
        yield_source_url="https://issuer.example/yield",
        maturity="2030-01-01",
        duration_years=3,
        credit_rating="ОФЗ",
        coupon_and_offer="Фиксированный купон",
        accrued_interest_note="НКД включается в цену",
        exit_rule="Выход при изменении процентного сценария",
    ).model_dump()
    with pytest.raises(ValidationError, match="Единицы"):
        Idea.model_validate(payload)


def test_arbitrage_commissions_on_all_legs_and_capital(idea, quote, verdict, settings, now):
    idea.kind, idea.setup = "arbitrage", "relative_value"
    idea.prerequisites = ["Подтверждён заём второй стороны и обеспечение корзины"]
    idea.legs.append(
        idea.legs[0].model_copy(update={"ticker": "SBERP", "direction": "sell", "stop": 104, "targets": [90]})
    )
    other = quote.model_copy(update={"ticker": "SBERP"})
    idea.arbitrage = ArbitrageDetails(
        gross_edge_bps=30,
        other_costs_bps=5,
        max_loss_rub=30,
        capital_rub=100,
        calculation="Расчёт одной корзины из двух инструментов с хеджированием.",
        execution_conditions="Синхронный вход в обе стороны с подтверждением доступной глубины.",
        borrow_and_margin="Заём второго инструмента должен быть доступен",
    )
    ranked, reason = rank(idea, verdict, [quote, other], settings, now, [])
    assert reason is None
    assert ranked.net_edge_bps == pytest.approx(4.9)
    idea.arbitrage.gross_edge_bps = 20
    assert "съедена" in rank(idea, verdict, [quote, other], settings, now, [])[1]


def test_arbitrage_stale_book_rejected(idea, quote, verdict, settings, now):
    idea.kind = "arbitrage"
    idea.legs.append(idea.legs[0].model_copy(update={"ticker": "SBERP"}))
    idea.arbitrage = ArbitrageDetails(
        gross_edge_bps=50,
        other_costs_bps=5,
        max_loss_rub=100,
        capital_rub=1000,
        calculation="Расчёт спреда на общий капитал арбитражной корзины.",
        execution_conditions="Исполнение обеих сторон с подтверждением глубины рынка.",
        borrow_and_margin="Требования к обеспечению обеих сторон сделки",
    )
    quote.book_at = now - timedelta(minutes=21)
    other = quote.model_copy(update={"ticker": "SBERP"})
    assert "стакана" in rank(idea, verdict, [quote, other], settings, now, [])[1]
