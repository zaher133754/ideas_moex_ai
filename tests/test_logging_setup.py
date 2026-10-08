import logging

from scout.config import Settings
from scout.logging_setup import RedactingFormatter


def test_token_redacted_from_message_and_traceback():
    token = "123:test-private-token"
    formatter = RedactingFormatter(token)
    try:
        raise RuntimeError("Request failed https://api.telegram.org/bot" + token + "/sendMessage")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "Transport error using %s", (token,), sys.exc_info()
        )
    formatted = formatter.format(record)
    assert token not in formatted
    assert "[REDACTED]" in formatted


def test_token_is_not_in_settings_repr():
    settings = Settings(telegram_bot_token="test-private-token")
    assert "test-private-token" not in repr(settings)
