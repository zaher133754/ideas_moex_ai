import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from scout.models import Leg, Quote

MOSCOW = ZoneInfo("Europe/Moscow")
MARKETS = {
    "TQBR": ("stock", "shares"),
    "TQTF": ("stock", "shares"),
    "TQCB": ("stock", "bonds"),
    "TQOB": ("stock", "bonds"),
    "RFUD": ("futures", "forts"),
}


def rows(payload: dict, key: str) -> list[dict]:
    block = payload.get(key, {})
    return [dict(zip(block.get("columns", []), row, strict=True)) for row in block.get("data", [])]


def positive(value):
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


class QuoteError(RuntimeError):
    pass


class MoexClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.semaphore = asyncio.Semaphore(4)

    async def get(self, path: str, params: dict | None = None) -> dict:
        async with self.semaphore:
            for attempt in range(3):
                try:
                    response = await self.client.get(
                        "https://iss.moex.com/iss/" + path, params={"iss.meta": "off", **(params or {})}
                    )
                    response.raise_for_status()
                    return response.json()
                except (httpx.HTTPError, ValueError):
                    if attempt == 2:
                        raise QuoteError("MOEX ISS недоступен или вернул неверные данные") from None
                    await asyncio.sleep(0.5 * 2**attempt)
        raise QuoteError("MOEX ISS недоступен")

    async def quote(self, leg: Leg, now: datetime, candles: bool = True) -> Quote:
        engine, market = MARKETS[leg.board]
        base = f"engines/{engine}/markets/{market}/boards/{leg.board}/securities/{leg.ticker}"
        payload = await self.get(base + ".json")
        securities, marketdata = rows(payload, "securities"), rows(payload, "marketdata")
        if not securities:
            raise QuoteError(f"{leg.ticker}: инструмент не найден на {leg.board}")
        sec = securities[0]
        md = marketdata[0] if marketdata else {}
        price = positive(md.get("LAST"))
        price_at = None
        turnover = None
        if price and positive(md.get("NUMTRADES")):
            timestamp = md.get("TIME") or md.get("UPDATETIME")
            if timestamp:
                stamp = str(timestamp)
                if "-" not in stamp:
                    date = md.get("TRADEDATE") or str(md.get("SYSTIME", ""))[:10]
                    if len(date) != 10:
                        raise QuoteError(f"{leg.ticker}: нет даты последней сделки")
                    stamp = f"{date} {stamp}"
                price_at = datetime.fromisoformat(stamp).replace(tzinfo=MOSCOW)
                turnover = positive(md.get("VALTODAY")) or positive(md.get("VALTODAY_RUR")) or 0
        if not price or price_at is None:
            history = await self.get(
                f"history/{base}.json",
                {
                    "from": (now - timedelta(days=10)).date().isoformat(),
                    "till": now.date().isoformat(),
                    "iss.only": "history",
                    "limit": 100,
                },
            )
            trades = [row for row in rows(history, "history") if positive(row.get("CLOSE"))]
            if not trades:
                raise QuoteError(f"{leg.ticker}: нет подтверждённой цены последней сессии")
            last = max(trades, key=lambda row: row["TRADEDATE"])
            price = positive(last["CLOSE"])
            # Daily history has no exact close time: use midnight conservatively and disclose it.
            price_at = datetime.fromisoformat(last["TRADEDATE"]).replace(tzinfo=MOSCOW)
            turnover = positive(last.get("VALUE")) or 0
        daily = []
        if candles and leg.board not in {"TQCB", "TQOB"}:
            data = await self.get(
                base + "/candles.json",
                {
                    "from": (now - timedelta(days=100)).date().isoformat(),
                    "till": now.date().isoformat(),
                    "interval": 24,
                },
            )
            daily = rows(data, "candles")[-100:]
            # Current turnover can be zero before opening; use most recent completed trading day.
            completed = [
                row
                for row in daily
                if str(row.get("begin", ""))[:10] < now.astimezone(MOSCOW).date().isoformat()
            ]
            if completed and (not turnover or turnover < 1):
                turnover = positive(completed[-1].get("value")) or 0
        currency = sec.get("CURRENCYID") or sec.get("FACEUNIT") or "RUB"
        if currency == "SUR":
            currency = "RUB"
        if currency != "RUB":
            raise QuoteError(f"{leg.ticker}: неподдерживаемая валюта {currency}, нельзя сравнить цены в RUB")
        multiplier = float(sec.get("LOTSIZE") or 1)
        if leg.board in {"TQCB", "TQOB"}:
            face = positive(sec.get("FACEVALUE"))
            if not face:
                raise QuoteError(f"{leg.ticker}: неизвестен номинал")
            multiplier *= face / 100
        elif leg.board == "RFUD":
            step, step_price = positive(sec.get("MINSTEP")), positive(sec.get("STEPPRICE"))
            if not step or not step_price:
                raise QuoteError(f"{leg.ticker}: неизвестна рублёвая стоимость шага")
            multiplier *= step_price / step
        book_at = None
        if md.get("SYSTIME") and md.get("UPDATETIME"):
            book_at = datetime.fromisoformat(str(md["SYSTIME"])[:10] + " " + str(md["UPDATETIME"])).replace(
                tzinfo=MOSCOW
            )
        return Quote(
            ticker=leg.ticker,
            board=leg.board,
            name=str(sec.get("SHORTNAME") or sec.get("SECNAME") or leg.ticker),
            price=price,
            price_at=price_at,
            fetched_at=now,
            daily_turnover_rub=turnover or 0,
            lot_size=int(sec.get("LOTSIZE") or 1),
            face_value=positive(sec.get("FACEVALUE")),
            accrued_interest=sec.get("ACCRUEDINT"),
            yield_pct=md.get("YIELD") or sec.get("YIELDATPREVWAPRICE"),
            bid=positive(md.get("BID")),
            ask=positive(md.get("OFFER")),
            currency=currency,
            book_at=book_at,
            trading_status=str(md.get("TRADINGSTATUS") or "unknown"),
            rub_per_price_unit_per_lot=multiplier,
            initial_margin_rub=positive(sec.get("INITIALMARGIN")) or positive(sec.get("BUYDEPO")),
            duration_days=positive(md.get("DURATION")),
            maturity=sec.get("MATDATE"),
            expiry=sec.get("LASTTRADEDATE"),
            source_url="https://iss.moex.com/iss/" + base + ".json",
            candles=daily,
        )
