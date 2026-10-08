import asyncio
from datetime import datetime

import httpx

from scout.codex import CodexAgent, ResearchError
from scout.config import Settings
from scout.models import Discovery, Evaluation, Report
from scout.moex import MoexClient
from scout.prompts import discovery_prompt, evaluation_prompt
from scout.ranking import eligibility, history_item, rank
from scout.reports import export
from scout.sources import PublicArticleReader
from scout.storage import Storage


class ScoutService:
    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self.lock = asyncio.Lock()
        self.agent = CodexAgent(settings)

    def now(self):
        return datetime.now(self.settings.tz)

    async def collect_quotes(self, ideas, candles=True):
        unique = {(leg.ticker, leg.board): leg for idea in ideas for leg in idea.legs}
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "ScoutTrading/0.1"}) as client:
            moex = MoexClient(client)
            results = await asyncio.gather(
                *(moex.quote(leg, self.now(), candles) for leg in unique.values()), return_exceptions=True
            )
        quotes, errors = {}, {}
        for key, result in zip(unique, results, strict=True):
            if isinstance(result, Exception):
                errors[key] = str(result)[:300]
            else:
                quotes[key] = result
        return quotes, errors

    async def research(self) -> Report:
        async with self.lock:
            now = self.now()
            day = now.date().isoformat()
            if self.storage.delivery(day) in {"sent", "sending", "uncertain"}:
                raise ResearchError(
                    "Сегодняшняя рассылка уже отправлена или её результат требует проверки /status"
                )
            self.storage.job_state(day, "running", now, increment=True)
            try:
                history = self.storage.history(now, self.settings.dedup_days)
                found = await self.agent.run(
                    discovery_prompt(self.settings, now, history[:200]), Discovery, "поиск"
                )
                articles = []
                if not found.ideas and self.agent.search_urls:
                    async with httpx.AsyncClient(
                        timeout=15, headers={"User-Agent": "ScoutTrading/0.1"}
                    ) as client:
                        articles = await PublicArticleReader(client).read_many(
                            self.agent.search_urls, self.now()
                        )
                    if articles:
                        found = await self.agent.run(
                            discovery_prompt(self.settings, self.now(), history[:200], articles),
                            Discovery,
                            "поиск с резервным чтением",
                        )
                now = self.now()
                if now.date().isoformat() != day:
                    raise ResearchError("Исследование пересекло полночь; требуется новый запуск")
                if abs((found.as_of - now).total_seconds()) > 7200:
                    raise ResearchError("Дата исследования не соответствует текущему дню")
                rejected, candidates = [], []
                candidate_history = list(history)
                ids = set()
                for idea in found.ideas:
                    reason = eligibility(idea, self.settings, now, candidate_history)
                    if idea.id in ids:
                        reason = "Повтор ID кандидата"
                    ids.add(idea.id)
                    if reason:
                        rejected.append({"title": idea.title, "reason": reason})
                    else:
                        candidates.append(idea)
                        candidate_history.append(history_item(idea))
                quotes, errors = await self.collect_quotes(candidates)
                checked = []
                for idea in candidates:
                    missing = [
                        errors[(x.ticker, x.board)] for x in idea.legs if (x.ticker, x.board) in errors
                    ]
                    if missing:
                        rejected.append({"title": idea.title, "reason": "; ".join(missing)})
                    else:
                        checked.append(idea)
                ranked, evaluation_notes = [], []
                if checked:
                    payload = {
                        "ideas": [i.model_dump(mode="json") for i in checked],
                        "quotes": [q.model_dump(mode="json") for q in quotes.values()],
                        "http_articles": articles,
                    }
                    evaluation = await self.agent.run(
                        evaluation_prompt(self.settings, self.now(), payload), Evaluation, "проверка"
                    )
                    evaluation_notes = evaluation.notes
                    verdicts = {v.idea_id: v for v in evaluation.verdicts}
                    if len(verdicts) != len(evaluation.verdicts) or set(verdicts) != {i.id for i in checked}:
                        raise ResearchError("Проверяющий агент вернул пропущенные, лишние или повторные ID")
                    now = self.now()
                    for idea in checked:
                        verdict = verdicts[idea.id]
                        if verdict.revised_legs:

                            def signature(leg):
                                return (leg.ticker, leg.board, leg.direction, leg.unit, leg.ratio)

                            if len(verdict.revised_legs) != len(idea.legs) or {
                                signature(x) for x in verdict.revised_legs
                            } != {signature(x) for x in idea.legs}:
                                rejected.append(
                                    {
                                        "title": idea.title,
                                        "reason": "Проверяющий изменил инструменты/направления/размеры корзины",
                                    }
                                )
                                continue
                            if not verdict.refined_entry_condition or not verdict.refined_invalidation:
                                rejected.append(
                                    {
                                        "title": idea.title,
                                        "reason": "Уточнённые уровни не сопровождаются условием входа/отмены",
                                    }
                                )
                                continue
                            idea = idea.model_copy(
                                update={
                                    "legs": verdict.revised_legs,
                                    "entry_condition": verdict.refined_entry_condition,
                                    "invalidation": verdict.refined_invalidation,
                                }
                            )
                        data = [quotes[(x.ticker, x.board)] for x in idea.legs]
                        item, reason = rank(idea, verdict, data, self.settings, now, history)
                        if item:
                            ranked.append(item)
                        else:
                            rejected.append({"title": idea.title, "reason": reason})
                ranked.sort(key=lambda r: r.score, reverse=True)
                report = Report(
                    day=day,
                    generated_at=self.now(),
                    researched_at=self.now(),
                    market_context=found.market_context,
                    ideas=ranked[: self.settings.report_size],
                    rejected=rejected,
                    notes=[
                        *found.search_notes,
                        *evaluation_notes,
                        "Даты публикаций и обоснования проверяет агент по открытым источникам; такая проверка может ошибаться.",
                        f"Комиссия для оценки: {self.settings.commission_bps:g} б.п. за операцию; замените на условия своего брокера.",
                    ],
                )
                self.storage.save_report(report)
                export(report, self.settings.data_dir / "reports")
                self.storage.job_state(day, "ready", self.now())
                return report
            except BaseException:
                self.storage.job_state(day, "failed", self.now())
                raise

    async def refresh(self, report: Report) -> Report:
        now = self.now()
        if report.day != now.date().isoformat():
            raise ResearchError("Нельзя отправить вчерашний отчёт как сегодняшний")
        quotes, errors = await self.collect_quotes([r.idea for r in report.ideas], candles=False)
        history = self.storage.history(now, self.settings.dedup_days)
        ranked, rejected = [], list(report.rejected)
        for old in report.ideas:
            missing = [errors[(x.ticker, x.board)] for x in old.idea.legs if (x.ticker, x.board) in errors]
            if missing:
                item, reason = None, "; ".join(missing)
            else:
                data = [quotes[(x.ticker, x.board)] for x in old.idea.legs]
                # Arbitrage economics must not survive changed prices without a fresh agent calculation.
                changed = old.idea.kind == "arbitrage" and any(
                    abs(q.price / previous.price - 1) > 0.0001
                    or q.bid != previous.bid
                    or q.ask != previous.ask
                    for q, previous in zip(data, old.quotes, strict=True)
                )
                if changed:
                    item, reason = None, "Арбитражные цены изменились после расчёта; нужна повторная оценка"
                else:
                    item, reason = rank(old.idea, old.verdict, data, self.settings, now, history)
                if item:
                    item.quotes = [
                        q.model_copy(update={"candles": previous.candles})
                        for q, previous in zip(item.quotes, old.quotes, strict=True)
                    ]
            if item:
                ranked.append(item)
            else:
                rejected.append({"title": old.idea.title, "reason": reason})
        ranked.sort(key=lambda r: r.score, reverse=True)
        result = report.model_copy(
            update={
                "ideas": ranked,
                "rejected": rejected,
                "generated_at": self.now(),
                "researched_at": report.researched_at or report.generated_at,
            }
        )
        self.storage.save_report(result)
        export(result, self.settings.data_dir / "reports")
        return result
