"""Async client for the BeatAPI capabilities API.

The public flow is ``search → inspect → run``; asynchronous capabilities return a
task id that is polled through ``GET /v1/tasks/{task_id}`` until it succeeds or
fails.

Design rules baked into this module (see https://docs.beatapi.io/capabilities):

* Search and Inspect are keyless; only Run (and result refetch) carry the key.
* A ``reference`` is never constructed locally — it must come from Search (or
  from an Inspect suggestion) and is validated against the published pattern.
* ``error.retryable == false`` is never retried; HTTP 429 honours
  ``Retry-After`` before the same call is attempted again.
* The API key is never logged, never embedded in an exception message and never
  sent to a media download URL.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final, Self
from urllib.parse import quote, urlparse

import httpx

from app.utils import (
    MediaFile,
    extension_for,
    extract_media,
    extract_output_text,
    filename_from_disposition,
    format_price,
    format_price_tiers,
    jitter_delay,
    safe_filename,
    url_basename,
)

if TYPE_CHECKING:
    from app.config import Settings

LOGGER = logging.getLogger(__name__)

BEATAPI_USER_AGENT: Final = "telegram-beatapi-bot/1.0 (+https://github.com/gase99/Bot)"
REFERENCE_PATTERN: Final = r"^(model|data|workflow):.+$"
_MAX_MESSAGE_CHARS: Final = 500

TASK_STATUSES: Final = frozenset(
    {
        "queued",
        "pending",
        "submitted",
        "started",
        "processing",
        "running",
        "storyboard_ready",
        "requires_action",
        "editing",
        "composing",
        "succeeded",
        "failed",
        "cancelled",
        "canceled",
        "expired",
    }
)
FAILED_STATUSES: Final = frozenset({"failed", "cancelled", "canceled", "expired"})
_RETRYABLE_HTTP_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

SleepFn = Callable[[float], Awaitable[None]]
ProgressFn = Callable[["TaskState"], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BeatAPIError(Exception):
    """A sanitised error returned by BeatAPI (or by the local client)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "api_error",
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        request_id: str | None = None,
        suggestions: Sequence[str] = (),
    ) -> None:
        self.message = _clean_message(message)
        self.code = code or "api_error"
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.request_id = request_id
        self.suggestions = tuple(str(item) for item in suggestions)[:10]
        super().__init__(self.message)

    def __str__(self) -> str:
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status_code is not None:
            parts.append(f"http={self.status_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status_code={self.status_code!r})"


class TaskFailedError(BeatAPIError):
    """A task reached a terminal failure state."""

    def __init__(self, task_id: str, message: str, **kwargs: Any) -> None:
        self.task_id = task_id
        super().__init__(message, **kwargs)


class TaskTimeoutError(BeatAPIError):
    """Polling exceeded the configured deadline while the task was still running."""

    def __init__(self, task_id: str, message: str, **kwargs: Any) -> None:
        self.task_id = task_id
        super().__init__(message, code="task_timeout", retryable=True, **kwargs)


def _clean_message(message: object) -> str:
    text = " ".join(str(message or "").split())
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[: _MAX_MESSAGE_CHARS - 1] + "…"
    return text or "Unknown BeatAPI error"


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class CapabilityCard:
    """One Search result card."""

    reference: str
    title: str
    summary: str = ""
    price: str = ""
    readiness: str = ""
    signature: str = ""
    kind: str = ""

    @property
    def label(self) -> str:
        return self.title or self.reference

    def to_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "title": self.title,
            "summary": self.summary,
            "price": self.price,
            "readiness": self.readiness,
            "signature": self.signature,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CapabilityCard:
        reference = str(data.get("reference") or "")
        return cls(
            reference=reference,
            title=str(data.get("title") or reference),
            summary=str(data.get("summary") or ""),
            price=str(data.get("price") or ""),
            readiness=str(data.get("readiness") or ""),
            signature=str(data.get("signature") or ""),
            kind=str(data.get("kind") or reference.split(":", 1)[0]),
        )


@dataclass(slots=True, frozen=True)
class SearchResult:
    """Parsed response of ``POST /v1/capabilities/search``."""

    cards: tuple[CapabilityCard, ...] = ()
    recommended_reference: str | None = None
    recommended_why: str = ""
    missing_inputs: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    groups: tuple[Mapping[str, Any], ...] = ()
    next_cursor: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.cards and not self.groups

    def pick(self) -> CapabilityCard | None:
        """Best card: the API recommendation when it is among the results."""
        if self.recommended_reference:
            for card in self.cards:
                if card.reference == self.recommended_reference:
                    return card
        return self.cards[0] if self.cards else None

    def find(self, reference: str) -> CapabilityCard | None:
        for card in self.cards:
            if card.reference == reference:
                return card
        return None


@dataclass(slots=True, frozen=True)
class CapabilityContract:
    """Parsed response of ``POST /v1/capabilities/inspect``."""

    reference: str
    title: str = ""
    description: str = ""
    readiness: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    pricing: Mapping[str, Any] = field(default_factory=dict)
    execution_mode: str = ""
    schema_hash: str = ""
    note: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title:
            object.__setattr__(self, "title", self.reference)

    @property
    def runnable(self) -> bool:
        return self.readiness.lower() not in {"listed"}

    @property
    def is_async(self) -> bool:
        return self.execution_mode.lower() == "async"

    @property
    def price_label(self) -> str:
        return format_price(self.pricing or {}) or "نامشخص"

    def price_tiers(self, limit: int = 4) -> list[str]:
        return format_price_tiers(self.pricing or {}, limit=limit)


@dataclass(slots=True, frozen=True)
class RunResult:
    """Parsed response of ``POST /v1/capabilities/run``."""

    reference: str
    payload: Mapping[str, Any]
    task_id: str | None = None
    status: str = ""
    output_text: str = ""
    request_id: str = ""
    truncated: bool = False
    result_ref: Mapping[str, Any] | None = None

    @property
    def is_async(self) -> bool:
        return self.task_id is not None


@dataclass(slots=True, frozen=True)
class TaskState:
    """One poll of ``GET /v1/tasks/{task_id}``."""

    task_id: str
    status: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    output: Any = None
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    poll_after_seconds: float | None = None
    media: tuple[MediaFile, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def failed(self) -> bool:
        return self.status in FAILED_STATUSES


@dataclass(slots=True, frozen=True)
class DownloadedFile:
    """Bytes downloaded from a result URL."""

    content: bytes
    mime_type: str = ""
    filename: str = "result.bin"

    @property
    def size(self) -> int:
        return len(self.content)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _next_action(payload: Mapping[str, Any]) -> str:
    next_block = payload.get("next")
    if isinstance(next_block, Mapping):
        action = next_block.get("action")
        if isinstance(action, str):
            return action.lower()
    return ""


def _next_note(payload: Mapping[str, Any]) -> str:
    for container in (payload, payload.get("data")):
        if isinstance(container, Mapping):
            next_block = container.get("next")
            if isinstance(next_block, Mapping):
                note = next_block.get("note")
                if isinstance(note, str):
                    return note
    return ""


def _envelope(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the API's ``data`` object when present, else the payload itself."""
    data = payload.get("data")
    if isinstance(data, Mapping):
        merged: dict[str, Any] = dict(data)
        merged.setdefault("next", payload.get("next"))
        return merged
    return payload


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", {}, []):
            return value
    return None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _parse_error(response: httpx.Response) -> BeatAPIError:
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    error = payload.get("error") if isinstance(payload, Mapping) else None
    error = error if isinstance(error, Mapping) else {}

    code = _as_str(error.get("code")) or f"http_{response.status_code}"
    message = _as_str(error.get("message"))
    if not message:
        message = f"BeatAPI responded with HTTP {response.status_code} {response.reason_phrase}".strip()

    retryable_raw = error.get("retryable")
    retryable = (
        bool(retryable_raw) if isinstance(retryable_raw, bool) else response.status_code in _RETRYABLE_HTTP_STATUS
    )
    if response.status_code == 429:
        # Rate limiting is always temporary; the wait is in Retry-After.
        retryable = True

    retry_after = _parse_retry_after(response.headers.get("retry-after"))
    if retry_after is None:
        body_delay = error.get("retry_after_seconds")
        if isinstance(body_delay, (int, float)) and not isinstance(body_delay, bool):
            retry_after = max(0.0, float(body_delay))

    suggestions = error.get("suggestions")
    request_id = _as_str(error.get("request_id")) or _as_str(response.headers.get("x-request-id"))

    return BeatAPIError(
        message,
        code=code,
        status_code=response.status_code,
        retryable=retryable,
        retry_after=retry_after,
        request_id=request_id or None,
        suggestions=suggestions if isinstance(suggestions, list) else (),
    )


def _parse_card(raw: Any) -> CapabilityCard | None:
    if not isinstance(raw, Mapping):
        return None
    reference = _as_str(raw.get("reference"))
    if not reference:
        return None
    return CapabilityCard(
        reference=reference,
        title=_as_str(raw.get("title")) or reference,
        summary=_as_str(raw.get("summary")) or _as_str(raw.get("description")),
        price=format_price(_first(raw.get("price"), raw.get("pricing"))),
        readiness=_as_str(raw.get("readiness")).lower(),
        signature=_as_str(raw.get("signature")),
        kind=_as_str(_first(raw.get("kind"), reference.split(":", 1)[0])),
    )


def _parse_search(payload: Mapping[str, Any]) -> SearchResult:
    envelope = _envelope(payload)
    cards_raw = envelope.get("data")
    if not isinstance(cards_raw, list):
        cards_raw = _first(
            envelope.get("results"),
            envelope.get("capabilities"),
            envelope.get("cards"),
            envelope.get("items"),
            [],
        )
    cards = tuple(card for card in (_parse_card(item) for item in cards_raw if isinstance(item, Mapping)) if card)

    recommended = envelope.get("recommended")
    recommended_reference: str | None = None
    recommended_why = ""
    missing_inputs: tuple[str, ...] = ()
    if isinstance(recommended, Mapping):
        recommended_reference = _as_str(recommended.get("reference")) or None
        recommended_why = _as_str(_first(recommended.get("why_match"), recommended.get("why")))
        raw_missing = recommended.get("missing_inputs")
        if isinstance(raw_missing, list):
            missing_inputs = tuple(str(item) for item in raw_missing)
    elif isinstance(recommended, str):
        recommended_reference = recommended.strip() or None

    hints_raw = envelope.get("hints")
    hints = tuple(str(item) for item in hints_raw) if isinstance(hints_raw, list) else ()
    understood = envelope.get("understood")
    terms: tuple[str, ...] = ()
    if isinstance(understood, Mapping):
        raw_terms = understood.get("terms")
        if isinstance(raw_terms, list):
            terms = tuple(str(item) for item in raw_terms)
    elif isinstance(understood, list):
        terms = tuple(str(item) for item in understood)

    groups_raw = envelope.get("groups")
    groups = tuple(item for item in groups_raw if isinstance(item, Mapping)) if isinstance(groups_raw, list) else ()

    if recommended_reference is None and cards:
        # Some responses omit ``recommended``; the first card is the best match.
        recommended_reference = cards[0].reference

    return SearchResult(
        cards=cards,
        recommended_reference=recommended_reference,
        recommended_why=recommended_why,
        missing_inputs=missing_inputs,
        hints=hints,
        terms=terms,
        groups=groups,
        next_cursor=_as_str(envelope.get("next_cursor")),
        raw=payload,
    )


def _parse_contract(reference: str, payload: Mapping[str, Any]) -> CapabilityContract:
    envelope = _envelope(payload)
    input_schema = envelope.get("input_schema")
    pricing = envelope.get("pricing")
    if not isinstance(pricing, Mapping):
        card_price = envelope.get("price")
        pricing = {"price": card_price} if card_price not in (None, "") else {}
    execution = envelope.get("execution")
    execution_mode = ""
    if isinstance(execution, Mapping):
        execution_mode = _as_str(execution.get("mode")).lower()

    return CapabilityContract(
        reference=_as_str(envelope.get("reference")) or reference,
        title=_as_str(_first(envelope.get("title"), envelope.get("name"))) or reference,
        description=_as_str(_first(envelope.get("description"), envelope.get("summary"))),
        readiness=_as_str(envelope.get("readiness")).lower(),
        input_schema=input_schema if isinstance(input_schema, Mapping) else {},
        pricing=pricing,
        execution_mode=execution_mode,
        schema_hash=_as_str(envelope.get("schema_hash")),
        note=_next_note(payload),
        raw=payload,
    )


_TASK_MARKERS: Final = ("task_kind", "capability_id", "poll_after_seconds", "output", "media_type", "workflow")


def _looks_like_task(payload: Mapping[str, Any]) -> bool:
    """Distinguish an async task envelope from a synchronous result."""
    if _next_action(payload) == "status":
        return True
    data = payload.get("data")
    if isinstance(data, Mapping):
        status = _as_str(data.get("status")).lower()
        if status in TASK_STATUSES and any(key in data for key in _TASK_MARKERS):
            return True
    status = _as_str(payload.get("status")).lower()
    return status in TASK_STATUSES and bool(_first(payload.get("task_id"), payload.get("id")))


def _parse_run(reference: str, payload: Mapping[str, Any]) -> RunResult:
    data = payload.get("data")
    task_data = data if isinstance(data, Mapping) else {}

    task_id = _as_str(_first(payload.get("task_id"), payload.get("id"), task_data.get("id"), task_data.get("task_id")))
    status = _as_str(_first(payload.get("status"), task_data.get("status"))).lower()
    request_id = _as_str(_first(payload.get("request_id"), task_data.get("request_id")))
    output_text = extract_output_text(payload)
    result_ref = payload.get("result_ref") or task_data.get("result_ref")

    is_task = _looks_like_task(payload)
    if not is_task:
        task_id = ""

    return RunResult(
        reference=reference,
        payload=payload,
        task_id=task_id or None,
        status=status,
        output_text=output_text,
        request_id=request_id,
        truncated=bool(payload.get("truncated") or task_data.get("truncated")),
        result_ref=result_ref if isinstance(result_ref, Mapping) else None,
    )


def _parse_task(task_id: str, payload: Mapping[str, Any]) -> TaskState:
    data = payload.get("data")
    source = data if isinstance(data, Mapping) and ("status" in data or "id" in data) else payload

    status = _as_str(_first(source.get("status"), payload.get("status"))).lower()
    output = _first(source.get("output"), payload.get("output"))
    error_code = _as_str(_first(source.get("error_code"), payload.get("error_code")))
    error_message = _as_str(
        _first(
            source.get("error_message"),
            payload.get("error_message"),
            (source.get("error") if isinstance(source.get("error"), Mapping) else {}).get("message"),
        )
    )
    retryable_raw = _first(source.get("retryable"), payload.get("retryable"))
    retryable = retryable_raw if isinstance(retryable_raw, bool) else False

    poll_after_raw = _first(source.get("poll_after_seconds"), payload.get("poll_after_seconds"))
    poll_after = (
        float(poll_after_raw)
        if isinstance(poll_after_raw, (int, float)) and not isinstance(poll_after_raw, bool)
        else None
    )

    return TaskState(
        task_id=task_id,
        status=status,
        payload=payload,
        output=output,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
        poll_after_seconds=poll_after,
        media=tuple(extract_media(payload, limit=10)),
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class BeatAPIClient:
    """Minimal, typed, retrying client for the three capability endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.beatapi.io",
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.5,
        max_retry_after_seconds: float = 30.0,
        poll_interval_min_seconds: float = 5.0,
        poll_interval_max_seconds: float = 10.0,
        poll_timeout_seconds: float = 900.0,
        max_download_bytes: int = 48 * 1024 * 1024,
        user_agent: str = BEATAPI_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("A BeatAPI API key is required")
        if poll_interval_max_seconds < poll_interval_min_seconds:
            raise ValueError("poll_interval_max_seconds must be >= poll_interval_min_seconds")

        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(0, int(max_retries))
        self._backoff = max(0.0, float(retry_backoff_seconds))
        self._max_retry_after = max(0.0, float(max_retry_after_seconds))
        self._poll_min = float(poll_interval_min_seconds)
        self._poll_max = float(poll_interval_max_seconds)
        self._poll_timeout = float(poll_timeout_seconds)
        self._max_download_bytes = int(max_download_bytes)
        self._sleep = sleep
        self._rng = rng or random.Random()

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(float(timeout_seconds), connect=min(15.0, float(timeout_seconds))),
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "X-Beat-Client": "http",
            },
            follow_redirects=True,
            transport=transport,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    def __repr__(self) -> str:
        return f"BeatAPIClient(base_url={self._base_url!r})"

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> BeatAPIClient:
        """Build a client from :class:`app.config.Settings`."""
        kwargs: dict[str, Any] = {
            "base_url": settings.beatapi_base_url,
            "timeout_seconds": settings.beatapi_timeout_seconds,
            "max_retries": settings.beatapi_max_retries,
            "retry_backoff_seconds": settings.beatapi_retry_backoff_seconds,
            "max_retry_after_seconds": settings.beatapi_max_retry_after_seconds,
            "poll_interval_min_seconds": settings.poll_interval_min_seconds,
            "poll_interval_max_seconds": settings.poll_interval_max_seconds,
            "poll_timeout_seconds": settings.poll_timeout_seconds,
            "max_download_bytes": settings.max_download_bytes,
        }
        kwargs.update(overrides)
        return cls(settings.beatapi_key, **kwargs)

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # --------------------------------------------------------------- helpers
    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _next_backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped at 30 seconds."""
        base = self._backoff * (2 ** max(0, attempt - 1))
        return min(30.0, base * self._rng.uniform(0.8, 1.2))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        auth: bool = False,
    ) -> Mapping[str, Any]:
        attempts = self._max_retries + 1
        last_error: BeatAPIError | None = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=dict(json_body) if json_body is not None else None,
                    headers=self._headers(auth=auth),
                )
            except httpx.HTTPError as exc:
                last_error = BeatAPIError(
                    f"ارتباط با BeatAPI برقرار نشد ({type(exc).__name__}).",
                    code="network_error",
                    retryable=True,
                )
                LOGGER.warning(
                    "BeatAPI %s %s network error on attempt %d/%d: %s",
                    method,
                    path,
                    attempt,
                    attempts,
                    type(exc).__name__,
                )
                if attempt < attempts:
                    await self._sleep(self._next_backoff(attempt))
                    continue
                raise last_error from exc

            elapsed_ms = (time.perf_counter() - started) * 1000
            LOGGER.info(
                "BeatAPI %s %s -> %d in %.0fms (attempt %d/%d)",
                method,
                path,
                response.status_code,
                elapsed_ms,
                attempt,
                attempts,
            )

            if response.status_code < 400:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise BeatAPIError(
                        "پاسخ BeatAPI قابل خواندن نبود (JSON نامعتبر).",
                        code="invalid_response",
                        status_code=response.status_code,
                        retryable=False,
                        request_id=response.headers.get("x-request-id"),
                    ) from exc
                return payload if isinstance(payload, Mapping) else {"data": payload}

            error = _parse_error(response)
            LOGGER.warning(
                "BeatAPI %s %s failed: http=%s code=%s retryable=%s request_id=%s",
                method,
                path,
                error.status_code,
                error.code,
                error.retryable,
                error.request_id or "-",
            )
            if not error.retryable:
                raise error
            if attempt >= attempts:
                raise error

            delay = self._next_backoff(attempt)
            if error.status_code == 429:
                if error.retry_after is not None:
                    if error.retry_after > self._max_retry_after:
                        LOGGER.warning(
                            "BeatAPI rate limit asks for %.1fs, above the %.1fs cap; not retrying",
                            error.retry_after,
                            self._max_retry_after,
                        )
                        raise error
                    delay = max(error.retry_after, 0.0)
                LOGGER.warning("BeatAPI rate limited; retrying in %.1fs", delay)
            await self._sleep(delay)

        raise last_error or BeatAPIError("درخواست به BeatAPI ناموفق بود.")

    # ------------------------------------------------------------ operations
    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        kind: str | None = None,
        platform: str | None = None,
        view: str = "compact",
    ) -> SearchResult:
        """Search the capability catalogue. Free and keyless."""
        text = (query or "").strip()
        if not text:
            raise BeatAPIError("عبارت جست‌وجو خالی است.", code="empty_query", retryable=False)

        body: dict[str, Any] = {"query": text, "limit": max(1, min(int(limit), 50))}
        if kind:
            body["kind"] = kind
        if platform:
            body["platform"] = platform
        if view:
            body["view"] = view

        payload = await self._request("POST", "/v1/capabilities/search", json_body=body)
        return _parse_search(payload)

    async def inspect(self, reference: str) -> CapabilityContract:
        """Read a capability contract. Free and keyless."""
        ref = self._validate_reference(reference)
        payload = await self._request("POST", "/v1/capabilities/inspect", json_body={"reference": ref})
        return _parse_contract(ref, payload)

    async def run(
        self,
        reference: str,
        inputs: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        view: str | None = None,
        max_items: int | None = None,
    ) -> RunResult:
        """Start a capability run (spends the account balance)."""
        ref = self._validate_reference(reference)
        body: dict[str, Any] = {
            "reference": ref,
            "operation": "start",
            "input": dict(inputs or {}),
            "idempotency_key": idempotency_key or secrets.token_hex(16),
        }
        if view:
            body["view"] = view
        if max_items is not None:
            body["max_items"] = max(1, min(int(max_items), 50))

        payload = await self._request("POST", "/v1/capabilities/run", json_body=body, auth=True)
        return _parse_run(ref, payload)

    async def status(self, reference: str, task_id: str) -> TaskState:
        """Fallback poll through ``capabilities/run`` (``operation=status``)."""
        ref = self._validate_reference(reference)
        payload = await self._request(
            "POST",
            "/v1/capabilities/run",
            json_body={"reference": ref, "operation": "status", "task_id": task_id},
            auth=True,
        )
        return _parse_task(task_id, payload)

    async def get_task(self, task_id: str, *, reference: str | None = None) -> TaskState:
        """Fetch one task through ``GET /v1/tasks/{task_id}``.

        If the shared task endpoint is not available for this task, the
        capability status operation is used as a fallback.
        """
        clean_id = (task_id or "").strip()
        if not clean_id:
            raise BeatAPIError("شناسه تسک خالی است.", code="invalid_task_id", retryable=False)

        try:
            payload = await self._request("GET", f"/v1/tasks/{quote(clean_id, safe='')}", auth=True)
        except BeatAPIError as exc:
            fallback_allowed = exc.status_code in {404, 405, 501} or exc.code in {
                "not_found",
                "http_404",
                "http_405",
            }
            if not fallback_allowed or not reference:
                raise
            LOGGER.info("Task endpoint unavailable (HTTP %s); falling back to capability status", exc.status_code)
            return await self.status(reference, clean_id)
        return _parse_task(clean_id, payload)

    async def wait_for_task(
        self,
        task_id: str,
        *,
        reference: str | None = None,
        timeout_seconds: float | None = None,
        on_progress: ProgressFn | None = None,
    ) -> TaskState:
        """Poll a task with 5–10 s jittered delays until it settles."""
        deadline = time.monotonic() + (self._poll_timeout if timeout_seconds is None else timeout_seconds)
        started = time.monotonic()
        state: TaskState | None = None
        polls = 0

        while True:
            state = await self.get_task(task_id, reference=reference)
            polls += 1
            LOGGER.info(
                "Task %s poll #%d -> %s (elapsed %.1fs)",
                task_id,
                polls,
                state.status or "unknown",
                time.monotonic() - started,
            )
            if on_progress is not None:
                try:
                    await on_progress(state)
                except Exception:  # pragma: no cover - UI errors must not kill polling
                    LOGGER.exception("Task progress callback failed")

            if state.succeeded:
                return state
            if state.failed:
                message = state.error_message or "تسک BeatAPI با خطا تمام شد."
                raise TaskFailedError(
                    task_id,
                    message,
                    code=state.error_code or "task_failed",
                    retryable=state.retryable,
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskTimeoutError(
                    task_id,
                    "زمان انتظار برای این تسک به پایان رسید؛ ممکن است هنوز در حال اجرا باشد.",
                )

            if state.poll_after_seconds and 1.0 <= state.poll_after_seconds <= 60.0:
                base = state.poll_after_seconds
                delay = base * self._rng.uniform(0.9, 1.2)
            else:
                delay = jitter_delay(self._poll_min, self._poll_max, self._rng)
            await self._sleep(min(delay, remaining))

    # Alias used by callers that prefer the shorter name.
    poll_task = wait_for_task

    async def download(self, url: str, *, max_bytes: int | None = None) -> DownloadedFile:
        """Download a result file without sending the API key to the CDN."""
        target = (url or "").strip()
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BeatAPIError("فقط لینک‌های http(s) قابل دانلود هستند.", code="invalid_url", retryable=False)

        limit = int(max_bytes if max_bytes is not None else self._max_download_bytes)
        try:
            async with self._client.stream("GET", target, headers={"Accept": "*/*"}, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise BeatAPIError(
                        f"دانلود فایل با HTTP {response.status_code} شکست خورد.",
                        code="download_failed",
                        status_code=response.status_code,
                        retryable=response.status_code >= 500,
                    )
                declared = response.headers.get("content-length")
                if declared and declared.strip().isdigit() and int(declared) > limit:
                    raise BeatAPIError(
                        "حجم فایل نتیجه از حد مجاز تلگرام بیشتر است.",
                        code="file_too_large",
                        retryable=False,
                    )
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > limit:
                        raise BeatAPIError(
                            "حجم فایل نتیجه از حد مجاز تلگرام بیشتر است.",
                            code="file_too_large",
                            retryable=False,
                        )
                mime = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                name = filename_from_disposition(response.headers.get("content-disposition"))
        except httpx.HTTPError as exc:
            raise BeatAPIError(
                "دانلود فایل نتیجه ناموفق بود.",
                code="download_failed",
                retryable=True,
            ) from exc

        filename = safe_filename(name or url_basename(target) or "result", "result")
        if "." not in filename:
            filename += extension_for(mime, "file")
        return DownloadedFile(content=bytes(buffer), mime_type=mime, filename=filename)

    # --------------------------------------------------------------- guards
    @staticmethod
    def _validate_reference(reference: str) -> str:
        ref = (reference or "").strip()
        if not re.match(REFERENCE_PATTERN, ref):
            raise BeatAPIError(
                "reference نامعتبر است؛ فقط مقادیر بازگشتی از Search قابل استفاده‌اند.",
                code="invalid_reference",
                retryable=False,
            )
        return ref
