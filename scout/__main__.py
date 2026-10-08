import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime

import httpx

from scout.config import Settings
from scout.locking import instance_lock
from scout.logging_setup import configure_logging
from scout.models import Leg
from scout.moex import MoexClient, rows
from scout.reports import export
from scout.service import ScoutService
from scout.storage import Storage


async def doctor(settings: Settings):
    binary = shutil.which(settings.codex_bin)
    print("Codex CLI:", binary or "не найден")
    if binary and not binary.lower().endswith((".cmd", ".bat", ".ps1")):
        status = await asyncio.to_thread(
            subprocess.run, [binary, "login", "status"], capture_output=True, text=True, timeout=20
        )
        print("Codex авторизация:", "есть" if status.returncode == 0 else "нужен codex login")
    print("Telegram токен:", "задан" if settings.telegram_bot_token else "не задан")
    print("Telegram владелец:", "задан" if settings.telegram_owner_id else "не задан")
    print("Рассылка:", settings.send_time.strftime("%H:%M"), settings.timezone)
    async with httpx.AsyncClient(timeout=25) as client:
        moex = MoexClient(client)
        for ticker, board, unit in (
            ("SBER", "TQBR", "RUB"),
            ("SU26238RMFS4", "TQOB", "percent_of_face"),
            ("SBMM", "TQBR", "RUB"),
        ):
            leg = Leg(
                ticker=ticker,
                board=board,
                direction="buy",
                unit=unit,
                ratio=1,
                entry_low=1,
                entry_high=2,
                stop=None,
                targets=[],
            )
            try:
                quote = await moex.quote(leg, datetime.now(settings.tz))
                print(
                    f"MOEX {ticker}: цена {quote.price:g}, время {quote.price_at.isoformat()}, свечей {len(quote.candles)}"
                )
            except Exception as exc:
                print(f"MOEX {ticker}: {type(exc).__name__}: {exc}")
        try:
            payload = await moex.get(
                "engines/futures/markets/forts/boards/RFUD/securities.json", {"iss.only": "securities"}
            )
            active = [
                item
                for item in rows(payload, "securities")
                if item.get("ASSETCODE") in {"Si", "SR", "SBRF"}
                and str(item.get("LASTTRADEDATE", "")) > datetime.now(settings.tz).date().isoformat()
            ]
            active.sort(key=lambda item: item["LASTTRADEDATE"])
            if active:
                leg = Leg(
                    ticker=active[0]["SECID"],
                    board="RFUD",
                    direction="buy",
                    unit="points",
                    ratio=1,
                    entry_low=1,
                    entry_high=2,
                    stop=None,
                    targets=[],
                )
                quote = await moex.quote(leg, datetime.now(settings.tz))
                print(
                    f"MOEX фьючерс {quote.ticker}: цена {quote.price:g}, ГО {quote.initial_margin_rub}, экспирация {quote.expiry}"
                )
            else:
                print("MOEX фьючерсы: контрольный активный контракт не найден")
        except Exception as exc:
            print(f"MOEX фьючерсы: {type(exc).__name__}: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Scout Trading: Codex CLI → Telegram")
    parser.add_argument(
        "command", choices=["bot", "doctor", "selftest", "research", "export"], nargs="?", default="bot"
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--research-now",
        action="store_true",
        help="При запуске бота сразу провести поиск и отправить результат владельцу",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.load(args.env_file)
        configure_logging(settings)
        if args.command == "bot":
            from scout.bot import TradingBot

            TradingBot(settings).run(research_now=args.research_now)
        elif args.command == "doctor":
            asyncio.run(doctor(settings))
        elif args.command == "selftest":
            from scout.diagnostics import telegram_selftest

            result = asyncio.run(telegram_selftest(settings))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["passed"]:
                raise SystemExit(1)
        else:
            storage = Storage(settings.data_dir)
            with instance_lock(settings.data_dir / "bot.lock"):
                if args.command == "research":
                    report = asyncio.run(ScoutService(settings, storage).research())
                    print(
                        f"Отчёт за {report.day}: {len(report.ideas)} идей, {len(report.rejected)} отклонено"
                    )
                else:
                    report = storage.report()
                    if report is None:
                        raise ValueError("Нет отчёта. Выполните scout research")
                paths = export(report, settings.data_dir / "reports")
                for path in paths.values():
                    print(path)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Validation errors could contain env values: only explicit operational errors are printed.
        from scout.codex import ResearchError

        if isinstance(exc, (ResearchError, RuntimeError)):
            print(str(exc), file=sys.stderr)
        else:
            print(f"Ошибка {type(exc).__name__}. Проверьте настройки и доступ к сети.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
