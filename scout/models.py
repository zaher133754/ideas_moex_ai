from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Source(StrictModel):
    url: HttpUrl
    title: str = Field(min_length=3, max_length=300)
    publisher: str = Field(min_length=2, max_length=100)
    published_at: AwareDatetime
    checked_at: AwareDatetime
    date_evidence: str = Field(min_length=10, max_length=700)
    supports: str = Field(min_length=10, max_length=1000)
    primary: bool


class Leg(StrictModel):
    ticker: str = Field(pattern=r"^[A-Za-z0-9_-]{1,24}$")
    board: Literal["TQBR", "TQCB", "TQOB", "TQTF", "RFUD"]
    direction: Literal["buy", "sell"]
    unit: Literal["RUB", "percent_of_face", "points"]
    ratio: float = Field(gt=0, le=1_000_000)
    entry_low: float | None = Field(gt=0)
    entry_high: float | None = Field(gt=0)
    stop: float | None
    targets: list[float] = Field(max_length=3)

    @model_validator(mode="after")
    def prices(self):
        # FORTS SECID is case-sensitive (e.g. SiZ6); stock SECID convention is uppercase.
        if self.board != "RFUD":
            self.ticker = self.ticker.upper()
        if (self.entry_low is None) != (self.entry_high is None):
            raise ValueError("Нужны обе границы входа или обе null")
        if self.entry_low is not None and self.entry_low > self.entry_high:
            raise ValueError("Перепутаны границы входа")
        if any(t <= 0 for t in self.targets) or (self.stop is not None and self.stop <= 0):
            raise ValueError("Уровни должны быть положительными")
        return self


class BondDetails(StrictModel):
    yield_to_maturity_pct: float | None
    yield_source_url: HttpUrl | None
    maturity: str
    duration_years: float | None
    credit_rating: str
    coupon_and_offer: str
    accrued_interest_note: str
    exit_rule: str = Field(min_length=15)


class ArbitrageDetails(StrictModel):
    gross_edge_bps: float = Field(gt=0)
    other_costs_bps: float = Field(ge=0)
    max_loss_rub: float = Field(gt=0)
    capital_rub: float = Field(gt=0)
    calculation: str = Field(min_length=30)
    execution_conditions: str = Field(min_length=30)
    borrow_and_margin: str = Field(min_length=15)


class Idea(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    title: str = Field(min_length=5, max_length=160)
    kind: Literal["stock", "bond", "fund", "future", "arbitrage"]
    setup: Literal["event", "technical", "relative_value"]
    catalyst_key: str = Field(min_length=8, max_length=200)
    catalyst_at: AwareDatetime | None
    discovered_at: AwareDatetime
    valid_until: AwareDatetime
    horizon: str = Field(min_length=3, max_length=100)
    thesis: str = Field(min_length=40, max_length=5000)
    entry_condition: str = Field(min_length=15, max_length=1500)
    invalidation: str = Field(min_length=15, max_length=1500)
    risks: list[str] = Field(min_length=2, max_length=10)
    prerequisites: list[str] = Field(max_length=10)
    leverage: float = Field(ge=1, le=20)
    margin_and_borrow: str = Field(min_length=10, max_length=2000)
    legs: list[Leg] = Field(min_length=1, max_length=4)
    sources: list[Source] = Field(min_length=1, max_length=8)
    bond: BondDetails | None
    arbitrage: ArbitrageDetails | None

    @model_validator(mode="after")
    def kind_consistency(self):
        expected = {"stock": {"TQBR"}, "fund": {"TQBR", "TQTF"}, "future": {"RFUD"}, "bond": {"TQCB", "TQOB"}}
        if self.kind in expected and (len(self.legs) != 1 or self.legs[0].board not in expected[self.kind]):
            raise ValueError("Тип идеи не соответствует инструменту")
        if self.kind == "bond" and self.bond is None:
            raise ValueError("Для облигации нужны её параметры")
        if self.kind == "arbitrage" and (self.arbitrage is None or len(self.legs) < 2):
            raise ValueError("Для арбитража нужны минимум две стороны и расчёт")
        if len({(x.ticker, x.board) for x in self.legs}) != len(self.legs):
            raise ValueError("Повтор стороны сделки")
        for leg in self.legs:
            unit = (
                "percent_of_face"
                if leg.board in {"TQCB", "TQOB"}
                else "points"
                if leg.board == "RFUD"
                else "RUB"
            )
            if leg.unit != unit:
                raise ValueError("Единицы цены не соответствуют режиму торгов")
        return self


class Discovery(StrictModel):
    as_of: AwareDatetime
    market_context: str = Field(max_length=5000)
    ideas: list[Idea] = Field(max_length=25)
    search_notes: list[str] = Field(max_length=20)


class Verdict(StrictModel):
    idea_id: str
    approved: bool
    evidence: float = Field(ge=0, le=10)
    thesis: float = Field(ge=0, le=10)
    risk: float = Field(ge=0, le=10)
    feasibility: float = Field(ge=0, le=10)
    source_dates_verified: bool
    levels_supported: bool
    reasoning: str = Field(min_length=30, max_length=3000)
    weaknesses: list[str] = Field(min_length=1, max_length=10)
    revised_legs: list[Leg] = Field(default_factory=list, max_length=4)
    refined_entry_condition: str | None = Field(default=None, min_length=15, max_length=1500)
    refined_invalidation: str | None = Field(default=None, min_length=15, max_length=1500)


class Evaluation(StrictModel):
    verdicts: list[Verdict] = Field(max_length=25)
    notes: list[str] = Field(max_length=20)


class Quote(StrictModel):
    ticker: str
    board: str
    name: str
    price: float = Field(gt=0)
    price_at: AwareDatetime
    fetched_at: AwareDatetime
    daily_turnover_rub: float = Field(ge=0)
    lot_size: int = Field(ge=1)
    face_value: float | None
    accrued_interest: float | None
    yield_pct: float | None
    bid: float | None
    ask: float | None
    currency: str
    book_at: AwareDatetime | None
    trading_status: str
    rub_per_price_unit_per_lot: float = Field(gt=0)
    initial_margin_rub: float | None
    duration_days: float | None
    maturity: str | None
    expiry: str | None
    source_url: HttpUrl
    candles: list[dict] = Field(max_length=100)


class RankedIdea(StrictModel):
    idea: Idea
    score: float
    components: dict[str, float]
    rr: float | None
    net_edge_bps: float | None
    verdict: Verdict
    quotes: list[Quote]
    fingerprint: str


class Report(StrictModel):
    day: str
    generated_at: datetime
    researched_at: AwareDatetime | None = None
    market_context: str
    ideas: list[RankedIdea]
    rejected: list[dict[str, str]]
    notes: list[str]
