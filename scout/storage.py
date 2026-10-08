import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from scout.models import Report
from scout.ranking import history_item


class Storage:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "scout.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS reports(day TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS seen(fingerprint TEXT PRIMARY KEY, sent_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(day TEXT PRIMARY KEY, state TEXT NOT NULL, message_id INTEGER);
                CREATE TABLE IF NOT EXISTS jobs(day TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def set(self, key: str, value: str):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def get(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_report(self, report: Report):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO reports VALUES (?, ?)", (report.day, report.model_dump_json()))

    def report(self, day: str | None = None) -> Report | None:
        with self.connect() as db:
            row = (
                db.execute("SELECT payload FROM reports WHERE day=?", (day,)).fetchone()
                if day
                else db.execute("SELECT payload FROM reports ORDER BY day DESC LIMIT 1").fetchone()
            )
        return Report.model_validate_json(row[0]) if row else None

    def history(self, now: datetime, days: int) -> list[dict]:
        cutoff = (now - timedelta(days=days)).timestamp()
        with self.connect() as db:
            records = db.execute("SELECT sent_at,payload FROM seen ORDER BY sent_at DESC").fetchall()
        return [
            json.loads(payload)
            for stamp, payload in records
            if datetime.fromisoformat(stamp).timestamp() >= cutoff
        ]

    def delivery(self, day: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT state FROM deliveries WHERE day=?", (day,)).fetchone()
        return row[0] if row else None

    def delivery_state(self, day: str, state: str):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?, ?, NULL)", (day, state))

    def delivered(self, report: Report, message_id: int, now: datetime):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?, 'sent', ?)", (report.day, message_id))
            for ranked in report.ideas:
                db.execute(
                    "INSERT OR REPLACE INTO seen VALUES (?, ?, ?)",
                    (
                        ranked.fingerprint,
                        now.isoformat(),
                        json.dumps(history_item(ranked.idea), ensure_ascii=False),
                    ),
                )

    def job(self, day: str) -> tuple[str, int, datetime] | None:
        with self.connect() as db:
            row = db.execute("SELECT state,attempts,updated_at FROM jobs WHERE day=?", (day,)).fetchone()
        return (row[0], row[1], datetime.fromisoformat(row[2])) if row else None

    def job_state(self, day: str, state: str, now: datetime, increment: bool = False):
        old = self.job(day)
        attempts = (old[1] if old else 0) + int(increment)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?)", (day, state, attempts, now.isoformat())
            )

    def reset_job(self, day: str):
        with self.connect() as db:
            db.execute("DELETE FROM jobs WHERE day=?", (day,))
