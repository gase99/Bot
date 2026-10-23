"""Entry point: ``python -m app.main``.

``MODE=webhook`` starts the FastAPI/uvicorn server (default), ``MODE=polling``
runs long polling — useful on GitHub Actions or a laptop behind NAT.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable

from pydantic import ValidationError
from telegram import Update
from telegram.error import NetworkError, TelegramError

from app.config import Settings

LOGGER = logging.getLogger(__name__)
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class SecretRedactingFilter(logging.Filter):
    """Replace known secret values with ``***`` before a record is formatted."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__(name="secret-redaction")
        unique = {secret for secret in secrets if secret and len(secret) >= 6}
        # Longest first, so a token that contains another token is fully masked.
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - defensive; never drop a log record
            return True
        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Format a record and scrub secrets from the final text (tracebacks included)."""

    def __init__(self, fmt: str, datefmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        unique = {secret for secret in secrets if secret and len(secret) >= 6}
        self._secrets = tuple(sorted(unique, key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***")
        return text


def setup_logging(level: str, secrets: Iterable[str] = ()) -> None:
    """Configure stdlib logging on stdout, with secret redaction."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT, "%Y-%m-%dT%H:%M:%S%z", secrets))
    handler.addFilter(SecretRedactingFilter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party libraries stay at WARNING or quieter, even in DEBUG mode.
    noisy_level = max(logging.WARNING, root.level)
    for name in ("httpx", "httpcore", "uvicorn.error", "telegram.ext.Application"):
        logging.getLogger(name).setLevel(noisy_level)


def _format_validation_error(error: ValidationError) -> str:
    """Describe configuration problems without echoing secret values."""
    lines = [
        "❌ Configuration is invalid.",
        "   Fill in the variables below (copy .env.example to .env) and start again:",
        "",
    ]
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "settings"
        lines.append(f"   • {location.upper()}: {item.get('msg', 'invalid value')}")
    lines += ["", "   See README.md → Quick Start for the required values."]
    return "\n".join(lines)


def run_polling(settings: Settings) -> None:
    """Long polling, with optional graceful self-stop for the 6h Actions limit."""
    from app.handlers import build_application

    application = build_application(settings)
    LOGGER.info("Starting Telegram bot in polling mode")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=True,
    )


def run_webhook(settings: Settings) -> None:
    """Serve ``POST /webhook`` with uvicorn."""
    import uvicorn

    from app.webhook import create_app

    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_config=None,
        log_level=None,
        access_log=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=30,
    )
    server = uvicorn.Server(config)
    LOGGER.info("Starting webhook server on 0.0.0.0:%d (path %s)", settings.port, settings.webhook_path)
    server.run()


def main() -> int:
    """Load settings, configure logging and run the selected mode."""
    try:
        settings = Settings()
    except ValidationError as exc:
        print(_format_validation_error(exc), file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.secrets_to_redact)
    LOGGER.info("Configuration loaded (mode=%s, log_level=%s)", settings.mode, settings.log_level)

    if settings.mode == "webhook" and "your.domain" in settings.webhook_url:
        LOGGER.warning(
            "WEBHOOK_URL still points at the placeholder 'your.domain'; Telegram cannot reach "
            "this bot. Set a public HTTPS URL, or use MODE=polling for local testing."
        )

    try:
        if settings.mode == "webhook":
            run_webhook(settings)
        else:
            run_polling(settings)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        LOGGER.info("Interrupted by the user; shutting down")
    except NetworkError:
        LOGGER.exception("Could not reach the Telegram Bot API; check connectivity")
        return 1
    except TelegramError:
        LOGGER.exception("Telegram rejected the bot token; check TELEGRAM_BOT_TOKEN")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
