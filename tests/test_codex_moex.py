import json
from datetime import timedelta

import httpx

from scout.codex import strict_schema
from scout.models import Discovery, Leg
from scout.moex import MoexClient


def test_codex_schema_is_supported_and_objects_strict():
    schema = strict_schema(Discovery)
    encoded = json.dumps(schema)
    assert '"format"' not in encoded

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)


def test_one_letter_stock_ticker_is_supported():
    leg = Leg(
        ticker="T",
        board="TQBR",
        direction="buy",
        unit="RUB",
        ratio=1,
        entry_low=None,
        entry_high=None,
        stop=None,
        targets=[],
    )
    assert leg.ticker == "T"


async def test_moex_uses_previous_session_if_no_trades_and_preserves_percent_price(now):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if "/history/" in request.url.path:
            data = {
                "history": {
                    "columns": ["TRADEDATE", "CLOSE", "VALUE"],
                    "data": [["2026-10-06", 95.2, 2_000_000]],
                }
            }
        else:
            data = {
                "securities": {
                    "columns": ["SECID", "FACEVALUE", "LOTSIZE", "ACCRUEDINT", "CURRENCYID", "MATDATE"],
                    "data": [["SU26238RMFS4", 1000, 1, 25, "SUR", "2041-05-15"]],
                },
                "marketdata": {
                    "columns": ["LAST", "NUMTRADES", "UPDATETIME", "SYSTIME"],
                    "data": [[95.3, 0, "07:59:00", "2026-10-07 07:59:00"]],
                },
            }
        return httpx.Response(200, json=data)

    leg = Leg(
        ticker="SU26238RMFS4",
        board="TQOB",
        direction="buy",
        unit="percent_of_face",
        ratio=1,
        entry_low=94,
        entry_high=96,
        stop=None,
        targets=[],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        quote = await MoexClient(client).quote(leg, now)
    assert quote.price == 95.2
    assert quote.price_at.date().isoformat() == "2026-10-06"
    assert quote.rub_per_price_unit_per_lot == 10
    assert quote.accrued_interest == 25
    assert len(requests) == 2


async def test_futures_initial_margin_and_ruble_point_value(now):
    sys_time = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat(sep=" ")

    def respond(request):
        return httpx.Response(
            200,
            json={
                "securities": {
                    "columns": ["SECID", "MINSTEP", "STEPPRICE", "INITIALMARGIN", "LASTTRADEDATE"],
                    "data": [["SiZ6", 1, 1, 10_000, "2026-12-17"]],
                },
                "marketdata": {
                    "columns": [
                        "LAST",
                        "NUMTRADES",
                        "TIME",
                        "TRADEDATE",
                        "UPDATETIME",
                        "SYSTIME",
                        "VALTODAY",
                    ],
                    "data": [[90_000, 30, "08:00:00", "2026-10-07", "08:00:00", sys_time, 1_000_000]],
                },
            },
        )

    leg = Leg(
        ticker="SiZ6",
        board="RFUD",
        direction="buy",
        unit="points",
        ratio=1,
        entry_low=89_000,
        entry_high=90_000,
        stop=88_000,
        targets=[95_000],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        quote = await MoexClient(client).quote(leg, now, candles=False)
    assert quote.initial_margin_rub == 10_000
    assert quote.rub_per_price_unit_per_lot == 1
    assert quote.expiry == "2026-12-17"
    assert leg.ticker == "SiZ6"
