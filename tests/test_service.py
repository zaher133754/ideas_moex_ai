from unittest.mock import AsyncMock

from scout.models import Discovery, Evaluation
from scout.service import ScoutService
from scout.storage import Storage


async def test_pipeline_with_real_structures_and_fresh_quote_refresh(idea, quote, verdict, settings, now):
    service = ScoutService(settings, Storage(settings.data_dir))
    service.now = lambda: now
    service.agent.run = AsyncMock(
        side_effect=[
            Discovery(as_of=now, market_context="Тестовый контекст", ideas=[idea], search_notes=[]),
            Evaluation(verdicts=[verdict], notes=[]),
        ]
    )
    service.collect_quotes = AsyncMock(return_value=({("SBER", "TQBR"): quote}, {}))
    report = await service.research()
    assert len(report.ideas) == 1
    assert service.agent.run.await_count == 2
    quote.price = 120
    refreshed = await service.refresh(report)
    assert not refreshed.ideas
    assert "далеко" in refreshed.rejected[-1]["reason"]
    assert service.storage.report(report.day) == refreshed


async def test_unavailable_quotes_remove_candidate_before_evaluation(idea, settings, now):
    service = ScoutService(settings, Storage(settings.data_dir))
    service.now = lambda: now
    service.agent.run = AsyncMock(
        return_value=Discovery(as_of=now, market_context="", ideas=[idea], search_notes=[])
    )
    service.collect_quotes = AsyncMock(return_value=({}, {("SBER", "TQBR"): "MOEX недоступен"}))
    report = await service.research()
    assert not report.ideas
    assert service.agent.run.await_count == 1
    assert "MOEX" in report.rejected[0]["reason"]


async def test_event_candidate_gets_plan_from_verified_candles(idea, quote, verdict, settings, now):
    original = idea.legs[0].model_copy()
    idea.legs[0].entry_low = idea.legs[0].entry_high = idea.legs[0].stop = None
    idea.legs[0].targets = []
    verdict.revised_legs = [original]
    verdict.refined_entry_condition = "Условный вход после удержания поддержки по свечам MOEX"
    verdict.refined_invalidation = "Отмена при пробое подтверждённого уровня стопа"
    service = ScoutService(settings, Storage(settings.data_dir))
    service.now = lambda: now
    service.agent.run = AsyncMock(
        side_effect=[
            Discovery(as_of=now, market_context="", ideas=[idea], search_notes=[]),
            Evaluation(verdicts=[verdict], notes=[]),
        ]
    )
    service.collect_quotes = AsyncMock(return_value=({("SBER", "TQBR"): quote}, {}))
    report = await service.research()
    assert len(report.ideas) == 1
    assert report.ideas[0].idea.legs[0].entry_low == 100
    assert "Условный" in report.ideas[0].idea.entry_condition


async def test_evaluator_cannot_replace_asset(idea, quote, verdict, settings, now):
    verdict.revised_legs = [idea.legs[0].model_copy(update={"ticker": "GAZP"})]
    verdict.refined_entry_condition = "Условие нового входа после подтверждения"
    verdict.refined_invalidation = "Отмена идеи при изменении исходного тезиса"
    service = ScoutService(settings, Storage(settings.data_dir))
    service.now = lambda: now
    service.agent.run = AsyncMock(
        side_effect=[
            Discovery(as_of=now, market_context="", ideas=[idea], search_notes=[]),
            Evaluation(verdicts=[verdict], notes=[]),
        ]
    )
    service.collect_quotes = AsyncMock(return_value=({("SBER", "TQBR"): quote}, {}))
    report = await service.research()
    assert not report.ideas
    assert "изменил инструменты" in report.rejected[0]["reason"]


async def test_quote_refresh_preserves_time_of_actual_research(settings, now):
    from scout.models import Report

    service = ScoutService(settings, Storage(settings.data_dir))
    service.now = lambda: now
    service.collect_quotes = AsyncMock(return_value=({}, {}))
    early = now.replace(hour=0)
    report = Report(
        day=now.date().isoformat(), generated_at=early, market_context="", ideas=[], rejected=[], notes=[]
    )
    refreshed = await service.refresh(report)
    assert refreshed.generated_at == now
    assert refreshed.researched_at == early
