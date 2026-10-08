import asyncio
import contextlib
import json
import logging
import os
from datetime import datetime, timedelta

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.error import Forbidden, NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from scout.config import Settings
from scout.reports import export, summary
from scout.service import ScoutService
from scout.storage import Storage

log = logging.getLogger(__name__)


def keyboard(day: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Скачать 5 идей", callback_data=f"report:{day}:5.html"),
                InlineKeyboardButton("Скачать 10 идей", callback_data=f"report:{day}:10.html"),
            ],
            [InlineKeyboardButton("Текст Markdown", callback_data=f"report:{day}:10.md")],
        ]
    )


class TradingBot:
    def __init__(self, settings: Settings):
        settings.require_telegram()
        self.settings = settings
        self.storage = Storage(settings.data_dir)
        self.service = ScoutService(settings, self.storage)
        self.background: set[asyncio.Task] = set()
        self.research_task: asyncio.Task | None = None
        self.scheduler_task: asyncio.Task | None = None
        self.publisher_task: asyncio.Task | None = None
        self.refreshed_day: str | None = None
        self.delivery_lock = asyncio.Lock()
        self.started_at: str | None = None
        self.bot_username: str | None = None
        self.research_on_start = False

    def heartbeat(self, now: datetime, state: str = "running"):
        self.storage.set(
            "runtime",
            json.dumps(
                {
                    "state": state,
                    "pid": os.getpid(),
                    "heartbeat_at": now.isoformat(),
                    "started_at": self.started_at,
                    "bot_username": self.bot_username,
                    "timezone": self.settings.timezone,
                    "send_time": self.settings.send_time.isoformat(),
                }
            ),
        )

    @property
    def chat_id(self):
        return self.settings.telegram_chat_id or int(
            self.storage.get("chat_id") or self.settings.telegram_owner_id
        )

    def authorized(self, update: Update) -> bool:
        user, chat = update.effective_user, update.effective_chat
        if not user or user.id != self.settings.telegram_owner_id or not chat:
            return False
        return chat.id in {self.settings.telegram_owner_id, self.settings.telegram_chat_id}

    async def guard(self, update: Update) -> bool:
        if self.authorized(update):
            return True
        if update.callback_query:
            await update.callback_query.answer("Доступ только владельцу", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Этот бот доступен только владельцу.")
        return False

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        if update.effective_chat.type == "private":
            self.storage.set("chat_id", str(update.effective_chat.id))
        await update.effective_message.reply_text(
            "Бот подключён. Ежедневная рассылка в "
            + self.settings.send_time.strftime("%H:%M")
            + " по Самаре.\nАкции, облигации, фонды, фьючерсы и арбитраж; лонги и шорты.\n"
            "/today — последний отчёт\n/search — подготовить сегодняшние идеи\n"
            "/report5 и /report10 — скачать разбор\n/status — состояние\n/retry — повторить неудачную доставку"
        )

    async def today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        report = self.storage.report()
        if report is None:
            await update.effective_message.reply_text(
                "Отчёта ещё нет. Используйте /search или дождитесь утреннего поиска."
            )
        else:
            await update.effective_message.reply_text(
                summary(report),
                reply_markup=keyboard(report.day),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    async def send_document(self, bot, chat_id: int, day: str | None, format_key: str):
        report = self.storage.report(day)
        if report is None:
            await bot.send_message(chat_id, "Отчёт ещё не подготовлен.")
            return
        paths = export(report, self.settings.data_dir / "reports")
        path = paths[format_key]
        with path.open("rb") as document:
            await bot.send_document(
                chat_id,
                document,
                filename=path.name,
                caption=f"Расширенный отчёт за {report.day}. Подходящих идей: {len(report.ideas)}.",
            )

    async def document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        size = 5 if update.effective_message.text.split()[0].split("@")[0] == "/report5" else 10
        await self.send_document(context.bot, update.effective_chat.id, None, f"{size}.html")

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        await update.callback_query.answer()
        _, day, key = update.callback_query.data.split(":")
        await self.send_document(context.bot, update.effective_chat.id, day, key)

    async def research_background(self, bot, notify_chat: int | None = None, send_file: bool = False):
        try:
            report = await self.service.research()
            if notify_chat:
                await bot.send_message(
                    notify_chat,
                    summary(report),
                    reply_markup=keyboard(report.day),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                if send_file:
                    await self.send_document(bot, notify_chat, report.day, "10.html")
            self.storage.set("last_research_completed_at", self.service.now().isoformat())
            log.info(
                "Исследование за %s завершено: %s идей, %s отклонено",
                report.day,
                len(report.ideas),
                len(report.rejected),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Исследование не завершено")
            self.storage.set("last_error", "Исследование не завершено; см. локальный журнал сервиса")
            if notify_chat:
                with contextlib.suppress(TelegramError):
                    await bot.send_message(
                        notify_chat,
                        "Поиск завершился ошибкой. Старые идеи не будут отправлены как новые. /status — состояние.",
                    )

    async def search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        if self.research_task and not self.research_task.done():
            await update.effective_message.reply_text("Поиск уже идёт. /status — состояние.")
            return
        day = self.service.now().date().isoformat()
        if self.storage.delivery(day) in {"sent", "sending", "uncertain"}:
            await update.effective_message.reply_text(
                "Сегодняшняя рассылка уже обработана. /today — посмотреть; /status — состояние доставки."
            )
            return
        await update.effective_message.reply_text(
            "Начинаю поиск и отдельную проверку источников. Это может занять до 45 минут с резервным чтением страниц."
        )
        self.research_task = self.spawn(self.research_background(context.bot, update.effective_chat.id))

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        now = self.service.now()
        day = now.date().isoformat()
        job = self.storage.job(day)
        delivery = self.storage.delivery(day) or "ожидается"
        report = self.storage.report(day)
        states = {"running": "идёт поиск", "failed": "ошибка поиска", "ready": "отчёт готов"}
        text = (
            f"Время сервиса: {now:%d.%m.%Y %H:%M:%S} Самара\n"
            f"Поиск: {states.get(job[0], job[0]) if job else 'ещё не запускался'}\n"
            f"Доставка: {delivery}\nИдей сегодня: {len(report.ideas) if report else 0}\n"
            f"Расписание: поиск {self.settings.research_time:%H:%M}, отправка {self.settings.send_time:%H:%M}\n"
            f"Свежесть ≤{self.settings.fresh_hours} ч; история повторов {self.settings.dedup_days} дней."
        )
        if delivery in {"uncertain", "sending"}:
            text += "\nTelegram не подтвердил результат. Проверьте чат; /retry может повторить сообщение."
        await update.effective_message.reply_text(text)

    async def retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.guard(update):
            return
        day = self.service.now().date().isoformat()
        if self.storage.delivery(day) == "sent":
            await update.effective_message.reply_text(
                "Сегодняшняя рассылка уже доставлена. /today покажет её повторно по запросу."
            )
            return
        if self.publisher_task and not self.publisher_task.done():
            await update.effective_message.reply_text("Отправка уже выполняется.")
            return
        if self.storage.delivery(day) is None:
            await update.effective_message.reply_text(
                "Доставка ещё не запускалась. /today — посмотреть отчёт; рассылка будет по расписанию."
            )
            return
        self.storage.delivery_state(day, "retry")
        await update.effective_message.reply_text("Повторяю доставку сегодняшнего отчёта.")
        self.publisher_task = self.spawn(self.publish(context.bot, force_refresh=True))

    async def publish(self, bot, force_refresh=False):
        async with self.delivery_lock:
            now = self.service.now()
            day = now.date().isoformat()
            if self.storage.delivery(day) in {"sent", "sending", "uncertain"}:
                return
            report = self.storage.report(day)
            research_at = datetime.combine(now.date(), self.settings.research_time, self.settings.tz)
            if report is None or (report.researched_at or report.generated_at) < research_at:
                notice = f"delay:{day}"
                if self.storage.get(notice) is None:
                    try:
                        await bot.send_message(
                            self.chat_id,
                            "Утренний отчёт ещё не готов: поиск не завершён или недоступен. Отправлю результат после проверки; вчерашние идеи не подставляю.",
                        )
                    except TelegramError:
                        log.warning("Не удалось доставить уведомление о задержке")
                    self.storage.set(notice, "1")
                return
            if force_refresh or self.refreshed_day != day or now - report.generated_at > timedelta(minutes=5):
                report = await self.service.refresh(report)
                self.refreshed_day = day
            self.storage.delivery_state(day, "sending")
            try:
                message = await bot.send_message(
                    self.chat_id,
                    summary(report),
                    reply_markup=keyboard(day),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except NetworkError:
                # Telegram has no idempotency key. Do not blindly resend a possibly accepted request.
                self.storage.delivery_state(day, "uncertain")
                log.warning("Результат доставки не подтверждён; проверьте чат и /retry")
                return
            except Forbidden:
                self.storage.delivery_state(day, "forbidden")
                log.warning("Бот заблокирован или чат не открыт командой /start")
                return
            except TelegramError:
                self.storage.delivery_state(day, "failed")
                log.warning("Telegram отклонил доставку")
                return
            self.storage.delivered(report, message.message_id, self.service.now())

    async def guarded_publish(self, bot):
        try:
            await self.publish(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Не удалось подготовить доставку")

    async def scheduler(self, bot):
        while True:
            try:
                await self._scheduler_loop(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка планировщика; восстановление через 5 секунд")
                await asyncio.sleep(5)

    async def _scheduler_loop(self, bot):
        last_publish_attempt = None
        last_heartbeat = None
        while True:
            now = self.service.now()
            if last_heartbeat is None or now - last_heartbeat >= timedelta(seconds=5):
                self.heartbeat(now)
                last_heartbeat = now
            day = now.date().isoformat()
            research_at = datetime.combine(now.date(), self.settings.research_time, self.settings.tz)
            send_at = datetime.combine(now.date(), self.settings.send_time, self.settings.tz)
            # On restart, catch up only on today's issue and only until noon.
            deadline = send_at + timedelta(hours=3)
            running = self.research_task and not self.research_task.done()
            job = self.storage.job(day)
            report = self.storage.report(day)
            morning_report = report and (report.researched_at or report.generated_at) >= research_at
            if job and job[2] < research_at and now >= research_at and not running and not morning_report:
                self.storage.reset_job(day)
                job = None
            should_retry = not job or (
                job[0] in {"failed", "running"} and job[1] < 3 and now - job[2] > timedelta(minutes=5)
            )
            if (
                research_at <= now < deadline
                and not running
                and not morning_report
                and should_retry
                and self.storage.delivery(day) not in {"sent", "sending", "uncertain"}
            ):
                self.research_task = self.spawn(self.research_background(bot))
            report = self.storage.report(day)
            refreshing = self.publisher_task and not self.publisher_task.done()
            if (
                send_at - timedelta(minutes=3) <= now < send_at
                and morning_report
                and self.refreshed_day != day
                and not refreshing
            ):

                async def refresh(report=report, day=day):
                    try:
                        await self.service.refresh(report)
                        self.refreshed_day = day
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("Не удалось обновить утренние котировки")

                self.publisher_task = self.spawn(refresh())
            state = self.storage.delivery(day)
            can_publish = state not in {"sent", "sending", "uncertain", "forbidden"}
            if send_at <= now < deadline and can_publish and not refreshing:
                if last_publish_attempt is None or now - last_publish_attempt >= timedelta(seconds=60):
                    last_publish_attempt = now
                    self.publisher_task = self.spawn(self.guarded_publish(bot))
            await asyncio.sleep(1)

    async def post_init(self, app: Application):
        self.started_at = self.service.now().isoformat()
        self.bot_username = app.bot.username
        await app.bot.set_my_commands(
            [
                BotCommand("today", "Последний отчёт"),
                BotCommand("search", "Поиск свежих идей"),
                BotCommand("report5", "Скачать 5 идей"),
                BotCommand("report10", "Скачать 10 идей"),
                BotCommand("status", "Состояние и расписание"),
                BotCommand("retry", "Повторить неудачную доставку"),
            ]
        )
        self.scheduler_task = asyncio.create_task(self.scheduler(app.bot))
        log.info(
            "Бот @%s запущен; поиск %s, рассылка %s %s",
            self.bot_username,
            self.settings.research_time,
            self.settings.send_time,
            self.settings.timezone,
        )
        if self.research_on_start:
            await app.bot.send_message(
                self.settings.telegram_owner_id,
                "Бот запущен локально. Начинаю полный тест поиска через Codex CLI; пришлю результат и расширенный файл после проверки. /status — состояние.",
            )
            self.research_task = self.spawn(
                self.research_background(
                    app.bot,
                    self.settings.telegram_owner_id,
                    send_file=True,
                )
            )

    async def post_stop(self, app: Application):
        tasks = [*self.background, *([self.scheduler_task] if self.scheduler_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.heartbeat(self.service.now(), "stopped")

    async def error_handler(self, update, context):
        # Avoid printing Telegram transport errors, which can contain credential-bearing URLs.
        log.error("Ошибка Telegram обработчика: %s", type(context.error).__name__)

    def run(self, research_now: bool = False):
        # Prevent multiple local services from polling/researching/publishing simultaneously.
        from scout.locking import instance_lock

        self.research_on_start = research_now
        with instance_lock(self.settings.data_dir / "bot.lock"):
            app = (
                Application.builder()
                .token(self.settings.telegram_bot_token)
                .post_init(self.post_init)
                .post_stop(self.post_stop)
                .build()
            )
            for name, handler in (
                ("start", self.start),
                ("help", self.start),
                ("today", self.today),
                ("search", self.search),
                ("report5", self.document),
                ("report10", self.document),
                ("status", self.status),
                ("retry", self.retry),
            ):
                app.add_handler(CommandHandler(name, handler))
            app.add_handler(
                CallbackQueryHandler(
                    self.callback, pattern=r"^report:\d{4}-\d{2}-\d{2}:(5\.html|10\.html|10\.md)$"
                )
            )
            app.add_error_handler(self.error_handler)
            app.run_polling(drop_pending_updates=False, allowed_updates=["message", "callback_query"])
