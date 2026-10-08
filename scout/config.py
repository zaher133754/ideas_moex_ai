import os
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_owner_id: int = 0
    telegram_chat_id: int | None = None
    data_dir: Path = Path("data")
    timezone: str = "Europe/Samara"
    send_time: time = time(9)
    research_time: time = time(8)
    fresh_hours: int = Field(48, ge=1, le=72)
    quote_max_hours: int = Field(96, ge=1, le=168)
    dedup_days: int = Field(90, ge=1)
    report_size: int = Field(10, ge=5, le=10)
    min_score: float = Field(65, ge=0, le=100)
    min_rr: float = Field(1.5, ge=1)
    min_daily_turnover_rub: float = Field(1_000_000, ge=0)
    max_entry_distance_pct: float = Field(3, ge=0, le=20)
    commission_bps: float = Field(5, ge=0)
    codex_model: str = ""
    codex_bin: str = "codex"
    codex_timeout_seconds: int = Field(900, ge=30, le=3600)
    allow_shorts: bool = True
    allow_futures: bool = True
    max_idea_leverage: float = Field(5, ge=1, le=20)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @model_validator(mode="after")
    def valid(self):
        ZoneInfo(self.timezone)
        if self.research_time >= self.send_time:
            raise ValueError("RESEARCH_TIME должен быть раньше SEND_TIME")
        return self

    @classmethod
    def load(cls, env_file: str = ".env"):
        env = {**dotenv_values(env_file), **os.environ}
        values = {}
        for name in cls.model_fields:
            value = env.get(name.upper())
            if value is not None and value != "":
                values[name] = value
        result = cls(**values)
        result.data_dir = result.data_dir.resolve()
        return result

    def require_telegram(self):
        if not self.telegram_bot_token or self.telegram_owner_id <= 0:
            raise ValueError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_OWNER_ID в .env")
