import logging
from logging.handlers import RotatingFileHandler

from scout.config import Settings


class RedactingFormatter(logging.Formatter):
    def __init__(self, token: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record):
        text = super().format(record)
        return text.replace(self.token, "[REDACTED]") if self.token else text


def configure_logging(settings: Settings):
    directory = settings.data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter(settings.telegram_bot_token)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            directory / "scout.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
