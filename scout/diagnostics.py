import hashlib
import json
from datetime import datetime

from telegram import Bot, LinkPreviewOptions
from telegram.error import TelegramError

from scout.bot import keyboard
from scout.config import Settings
from scout.reports import export
from scout.storage import Storage


async def telegram_selftest(settings: Settings) -> dict:
    """Exercise the real Telegram transport without marking the daily issue delivered."""
    settings.require_telegram()
    storage = Storage(settings.data_dir)
    result = {"started_at": datetime.now(settings.tz).isoformat(), "checks": {}}
    try:
        async with Bot(settings.telegram_bot_token) as bot:
            me = await bot.get_me()
            result["bot_username"] = me.username
            result["checks"]["authorization"] = True
            hook = await bot.get_webhook_info()
            result["checks"]["polling_has_no_webhook"] = not bool(hook.url)
            chat = await bot.get_chat(settings.telegram_owner_id)
            if chat.type != "private" or chat.id != settings.telegram_owner_id:
                raise RuntimeError("Для теста нужен личный чат владельца")
            storage.set("chat_id", str(chat.id))
            result["checks"]["owner_chat"] = True
            report = storage.report()
            body = (
                "✅ Тест Scout Trading\n\n"
                "Авторизация и личный чат проверены. Это тест доставки, а не утренний выпуск.\n"
                f"Поиск: {settings.research_time:%H:%M}; рассылка: {settings.send_time:%H:%M} по Самаре.\n"
                f"Свежесть источников: до {settings.fresh_hours} ч.\n"
                "Команды: /status, /today, /search, /report5, /report10."
            )
            if report:
                body += (
                    f"\n\nПоследний реальный отчёт: {report.day}; прошли отбор {len(report.ideas)}, "
                    f"отклонены {len(report.rejected)}. Тестовые сделки не добавлялись."
                )
            message = await bot.send_message(
                chat.id,
                body,
                reply_markup=keyboard(report.day) if report else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            result["checks"]["message_delivered"] = bool(message.message_id)
            if report:
                paths = export(report, settings.data_dir / "reports")
                for size in (5, 10):
                    path = paths[f"{size}.html"]
                    with path.open("rb") as file:
                        sent = await bot.send_document(
                            chat.id,
                            file,
                            filename=path.name,
                            caption=f"Тест файла на {size} идей. Реальный отчёт за {report.day}, прошли отбор: {len(report.ideas)}.",
                        )
                    remote = await bot.get_file(sent.document.file_id)
                    downloaded = await remote.download_as_bytearray()
                    same = hashlib.sha256(downloaded).digest() == hashlib.sha256(path.read_bytes()).digest()
                    result["checks"][f"report{size}_upload_download"] = same
            result["checks"]["schedule"] = (
                settings.timezone == "Europe/Samara" and str(settings.send_time) == "09:00:00"
            )
    except TelegramError as exc:
        # Transport exceptions can contain token-bearing URLs. Persist only their type.
        result["error_type"] = type(exc).__name__
    result["passed"] = all(result["checks"].values()) and "error_type" not in result
    directory = settings.data_dir / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "last-selftest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
