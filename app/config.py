"""Environment-based configuration.

Every value is read from the process environment, falling back to a local
``.env`` file (see ``.env.example``). Secrets are held as ``SecretStr`` so an
accidental ``repr(settings)`` never prints a token.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOG_LEVELS: tuple[str, ...] = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})
_USER_ID_SPLIT = re.compile(r"[,\s]+")


class Settings(BaseSettings):
    """Typed, validated runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ core
    telegram_bot_token: SecretStr = Field(description="Bot token issued by @BotFather.")
    beatapi_api_key: SecretStr = Field(description="BeatAPI key. Server-side only.")
    mode: Literal["webhook", "polling"] = "webhook"
    webhook_url: str = Field(default="", description="Public HTTPS URL ending in /webhook.")
    port: int = Field(default=8443, ge=1, le=65535)
    log_level: str = "INFO"

    # --------------------------------------------------------- BeatAPI client
    beatapi_base_url: str = "https://api.beatapi.io"
    beatapi_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    beatapi_max_retries: int = Field(default=2, ge=0, le=10)
    beatapi_retry_backoff_seconds: float = Field(default=1.5, ge=0.0, le=60.0)
    beatapi_max_retry_after_seconds: float = Field(default=30.0, ge=0.0, le=300)

    # ------------------------------------------------------------ task polling
    poll_interval_min_seconds: float = Field(default=5.0, ge=1.0, le=120.0)
    poll_interval_max_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    poll_timeout_seconds: float = Field(default=900.0, ge=30.0, le=7200.0)
    progress_update_seconds: float = Field(default=20.0, ge=5.0, le=300.0)

    # -------------------------------------------------------------- delivery
    max_download_bytes: int = Field(default=48 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    max_result_chars: int = Field(default=3500, ge=500, le=4096)
    max_attachments: int = Field(default=3, ge=0, le=10)

    # ---------------------------------------------------------------- webhook
    webhook_secret_token: SecretStr | None = None
    webhook_max_connections: int = Field(default=40, ge=1, le=100)

    # ------------------------------------------------------------ operation
    polling_max_runtime_seconds: int = Field(default=0, ge=0)
    allowed_user_ids: str = ""
    rate_limit_per_minute: int = Field(default=20, ge=0, le=10_000)
    concurrent_updates: int = Field(default=32, ge=1, le=256)
    session_ttl_seconds: int = Field(default=3600, ge=60)

    # ------------------------------------------------------------ validators
    @field_validator("telegram_bot_token", "beatapi_api_key")
    @classmethod
    def _require_secret(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not raw:
            raise ValueError(f"{info.field_name.upper()} is empty. Copy .env.example to .env and fill it in.")
        return SecretStr(raw)

    @field_validator("webhook_secret_token", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        text = str(value).strip()
        return SecretStr(text) if text else None

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> str:
        text = str(value or "INFO").strip().upper()
        if text not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
        return text

    @field_validator("beatapi_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("BEATAPI_BASE_URL must be an absolute http(s) URL")
        return url

    @field_validator("allowed_user_ids")
    @classmethod
    def _validate_allowed_ids(cls, value: str) -> str:
        parts = [part for part in _USER_ID_SPLIT.split(value.strip()) if part]
        for part in parts:
            if not part.lstrip("-").isdigit():
                raise ValueError(f"ALLOWED_USER_IDS must be comma-separated integers, got {part!r}")
        return ",".join(parts)

    @model_validator(mode="after")
    def _validate_mode(self) -> Settings:
        if self.poll_interval_max_seconds < self.poll_interval_min_seconds:
            raise ValueError("POLL_INTERVAL_MAX_SECONDS must be >= POLL_INTERVAL_MIN_SECONDS")

        if self.mode != "webhook":
            return self

        raw = self.webhook_url.strip()
        if not raw:
            raise ValueError(
                "WEBHOOK_URL is required when MODE=webhook "
                "(for example https://your.domain/webhook). Use MODE=polling to skip it."
            )
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("WEBHOOK_URL must be an absolute http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("WEBHOOK_URL must not contain a query string or a fragment")
        if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
            raise ValueError("WEBHOOK_URL must use https for public hosts")
        if parsed.path in {"", "/"}:
            raw = raw.rstrip("/") + "/webhook"
        self.webhook_url = raw.rstrip("/")
        return self

    # ------------------------------------------------------------ properties
    @property
    def telegram_token(self) -> str:
        return self.telegram_bot_token.get_secret_value()

    @property
    def beatapi_key(self) -> str:
        return self.beatapi_api_key.get_secret_value()

    @property
    def webhook_path(self) -> str:
        """Path component of ``WEBHOOK_URL`` (``/webhook`` by default)."""
        return urlparse(self.webhook_url).path or "/webhook"

    @property
    def webhook_secret(self) -> str:
        """Secret header value Telegram must echo back on every webhook call.

        Derived from the bot token when ``WEBHOOK_SECRET_TOKEN`` is unset, so a
        fresh deployment is authenticated without extra configuration.
        """
        if self.webhook_secret_token is not None:
            return self.webhook_secret_token.get_secret_value()
        digest = hashlib.sha256(f"telegram-beatapi-bot:{self.telegram_token}".encode()).hexdigest()
        return digest[:48]

    @property
    def allowed_user_id_set(self) -> frozenset[int]:
        """User ids allowed to talk to the bot (empty set = everyone)."""
        return frozenset(int(part) for part in self.allowed_user_ids.split(",") if part.strip().lstrip("-").isdigit())

    @property
    def secrets_to_redact(self) -> tuple[str, ...]:
        values = [self.telegram_token, self.beatapi_key, self.webhook_secret]
        return tuple(value for value in values if value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings for the current process."""
    return Settings()
