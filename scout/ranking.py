import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from scout.config import Settings
from scout.models import Idea, Quote, RankedIdea, Verdict


def canonical_url(value: str) -> str:
    url = urlsplit(value)
    query = [
        (k, v)
        for k, v in parse_qsl(url.query)
        if not k.lower().startswith("utm_") and k not in {"from", "ref"}
    ]
    return urlunsplit(
        (url.scheme.lower(), url.netloc.lower(), url.path.rstrip("/"), urlencode(sorted(query)), "")
    )


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w.]+", text.lower()))


def asset_key(idea: Idea) -> str:
    return "|".join(sorted(f"{leg.board}:{leg.ticker}:{leg.direction}" for leg in idea.legs))


def fingerprint(idea: Idea) -> str:
    raw = asset_key(idea) + ":" + " ".join(sorted(tokens(idea.catalyst_key)))
    return hashlib.sha256(raw.encode()).hexdigest()


def history_item(idea: Idea) -> dict:
    return {
        "fingerprint": fingerprint(idea),
        "assets": asset_key(idea),
        "catalyst_key": idea.catalyst_key,
        "title": idea.title,
        "setup": idea.setup,
        "sources": [canonical_url(str(s.url)) for s in idea.sources],
    }


def is_duplicate(idea: Idea, history: list[dict]) -> bool:
    current = history_item(idea)
    for old in history:
        if current["fingerprint"] == old.get("fingerprint"):
            return True
        if current["assets"] != old.get("assets"):
            continue
        if set(current["sources"]) & set(old.get("sources", [])):
            return True
        left, right = tokens(current["catalyst_key"]), tokens(old.get("catalyst_key", ""))
        if left and right and len(left & right) / len(left | right) >= 0.6:
            return True
        # A recycled technical pattern on the same instruments remains a duplicate even with new prices.
        if idea.setup == "technical" and old.get("setup") == "technical":

            def no_numbers(s):
                return {x for x in tokens(s) if not re.fullmatch(r"[\d.]+", x)}

            a, b = no_numbers(idea.catalyst_key), no_numbers(old.get("catalyst_key", ""))
            if a and b and len(a & b) / len(a | b) >= 0.8:
                return True
    return False


def eligibility(idea: Idea, settings: Settings, now: datetime, history: list[dict]) -> str | None:
    if idea.valid_until <= now:
        return "Истёк срок применимости"
    if idea.discovered_at > now + timedelta(minutes=5) or idea.discovered_at < now - timedelta(hours=24):
        return "Некорректное время исследования"
    if any(s.published_at > now or s.checked_at > now + timedelta(minutes=5) for s in idea.sources):
        return "Источник датирован будущим"
    fresh = [s for s in idea.sources if now - timedelta(hours=settings.fresh_hours) <= s.published_at <= now]
    if not fresh:
        return f"Нет подтверждающего источника за {settings.fresh_hours} часов"
    if any(s.checked_at < now - timedelta(hours=24) for s in idea.sources):
        return "Источники не проверены текущим исследованием"
    if idea.catalyst_at is not None and idea.catalyst_at <= now and idea.setup == "event":
        return "Событийный катализатор уже произошёл"
    if is_duplicate(idea, history):
        return "Идея уже отправлялась / повтор кандидата"
    if idea.leverage > settings.max_idea_leverage:
        return "Плечо превышает лимит"
    if not settings.allow_shorts and any(x.direction == "sell" for x in idea.legs):
        return "Шорты отключены"
    if not settings.allow_futures and any(x.board == "RFUD" for x in idea.legs):
        return "Фьючерсы отключены"
    return None


def rank(
    idea: Idea, verdict: Verdict, quotes: list[Quote], settings: Settings, now: datetime, history: list[dict]
) -> tuple[RankedIdea | None, str | None]:
    reason = eligibility(idea, settings, now, history)
    if reason:
        return None, reason
    if not verdict.approved or not verdict.source_dates_verified or not verdict.levels_supported:
        return None, "Критическая проверка агента не пройдена: " + verdict.reasoning[:300]
    mapping = {(q.ticker, q.board): q for q in quotes}
    rr = None
    for leg in idea.legs:
        if leg.entry_low is None or leg.entry_high is None:
            return None, "Нет подтверждённого плана входа"
        q = mapping.get((leg.ticker, leg.board))
        if not q:
            return None, "Не подтверждена котировка одной из сторон"
        if q.price_at > now + timedelta(minutes=5) or now - q.price_at > timedelta(
            hours=settings.quote_max_hours
        ):
            return None, "Котировка устарела или датирована будущим"
        if q.daily_turnover_rub < settings.min_daily_turnover_rub:
            return None, "Недостаточный дневной оборот"
        if q.expiry and q.expiry <= now.date().isoformat():
            return None, "Контракт истекает / истёк"
        if leg.board == "RFUD" and q.initial_margin_rub is None:
            return None, "Не подтверждено первоначальное ГО фьючерса"
        if q.maturity and q.maturity <= now.date().isoformat():
            return None, "Выпуск погашен / погашается сегодня"
        distance = max(leg.entry_low - q.price, q.price - leg.entry_high, 0) / q.price * 100
        if distance > settings.max_entry_distance_pct:
            return None, "Цена слишком далеко от диапазона входа"
        if leg.stop is not None:
            if leg.direction == "buy" and (leg.stop >= leg.entry_low or q.price <= leg.stop):
                return None, "Неверный / уже пробитый стоп покупки"
            if leg.direction == "sell" and (leg.stop <= leg.entry_high or q.price >= leg.stop):
                return None, "Неверный / уже пробитый стоп шорта"
        if leg.targets:
            if leg.direction == "buy" and any(t <= leg.entry_high or q.price >= t for t in leg.targets):
                return None, "Цель покупки некорректна / уже достигнута"
            if leg.direction == "sell" and any(t >= leg.entry_low or q.price <= t for t in leg.targets):
                return None, "Цель шорта некорректна / уже достигнута"
        if idea.leverage > 1 or leg.direction == "sell" or leg.board == "RFUD":
            if not idea.prerequisites:
                return None, "Не описаны условия обеспечения / заимствования"
        if idea.kind in {"stock", "fund", "future"}:
            if leg.stop is None or not leg.targets:
                return None, "Нет стопа или цели"
            entry = leg.entry_high if leg.direction == "buy" else leg.entry_low
            risk = abs(entry - leg.stop)
            target = min(leg.targets) if leg.direction == "buy" else max(leg.targets)
            reward = abs(target - entry)
            reward_costs = (entry + target) * settings.commission_bps / 10_000
            risk_costs = (entry + leg.stop) * settings.commission_bps / 10_000
            rr = (reward - reward_costs) / (risk + risk_costs)
            if rr < settings.min_rr:
                return None, "Недостаточное отношение дохода к риску после комиссий"
        if (
            idea.kind == "bond"
            and q.yield_pct is None
            and (idea.bond.yield_to_maturity_pct is None or idea.bond.yield_source_url is None)
        ):
            return None, "Не подтверждена доходность облигации"
    net = None
    if idea.kind == "arbitrage":
        arb = idea.arbitrage
        basket = 0.0
        books = []
        for leg in idea.legs:
            q = mapping[(leg.ticker, leg.board)]
            if (
                q.book_at is None
                or q.book_at > now + timedelta(minutes=2)
                or now - q.book_at > timedelta(minutes=20)
            ):
                return None, "Для арбитража нет свежего стакана"
            status_ok = q.trading_status == "T" or (q.board == "RFUD" and q.trading_status == "unknown")
            if (
                not status_ok
                or not q.bid
                or not q.ask
                or q.bid > q.ask
                or now - q.price_at > timedelta(minutes=20)
            ):
                return None, "Для арбитража нет исполнимого bid/ask"
            books.append(q.book_at)
            price = q.ask if leg.direction == "buy" else q.bid
            basket += price * q.rub_per_price_unit_per_lot * leg.ratio
        if max(books) - min(books) > timedelta(minutes=2):
            return None, "Котировки сторон несинхронны"
        commissions = 2 * settings.commission_bps * basket / arb.capital_rub
        net = arb.gross_edge_bps - arb.other_costs_bps - commissions
        if net <= 0:
            return None, "Арбитражная премия съедена расходами"
    freshest = max(s.published_at for s in idea.sources)
    age = (now - freshest).total_seconds() / 3600
    liquidity = min(q.daily_turnover_rub for q in quotes)
    components = {
        "Доказательства": round(verdict.evidence * 2.5, 2),
        "Логика": round(verdict.thesis * 2, 2),
        "Контроль риска": round(verdict.risk * 1.5, 2),
        "Исполнение": round(verdict.feasibility, 2),
        "Ликвидность": round(
            min(10, 5 + 5 * min(liquidity / max(settings.min_daily_turnover_rub * 20, 1), 1)), 2
        ),
        "Свежесть": round(10 * max(0, 1 - age / settings.fresh_hours), 2),
        "Параметры сделки": round(min(10, rr / 3 * 10) if rr is not None else verdict.risk, 2),
    }
    score = round(sum(components.values()), 1)
    if score < settings.min_score:
        return None, f"Рейтинг {score} ниже порога {settings.min_score}"
    return RankedIdea(
        idea=idea,
        score=score,
        components=components,
        rr=rr,
        net_edge_bps=net,
        verdict=verdict,
        quotes=quotes,
        fingerprint=fingerprint(idea),
    ), None
