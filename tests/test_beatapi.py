"""Unit tests for the BeatAPI client and the helpers it relies on.

Every HTTP call is served by ``httpx.MockTransport`` — no network access, no
Telegram token and no BeatAPI key are needed to run this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from typing import Any, Self

import httpx
import pytest
from pydantic import ValidationError
from telegram.ext import Application

from app.beatapi import (
    BeatAPIClient,
    BeatAPIError,
    DownloadedFile,
    TaskFailedError,
    TaskTimeoutError,
)
from app.config import Settings
from app.handlers import deliver_payload, run_command, search_command
from app.main import RedactingFormatter, SecretRedactingFilter
from app.utils import (
    UserRateLimiter,
    build_example_input,
    chunk_text,
    describe_input_schema,
    extract_links,
    extract_media,
    extract_output_text,
    format_price,
    parse_json_argument,
    validate_input,
)
from app.webhook import create_app

BASE_URL = "https://api.test"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class Harness:
    """Runs a BeatAPIClient against a scripted transport and records traffic."""

    def __init__(self, handler: Any, *, fake_sleep: bool = True, **kwargs: Any) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.fake_sleep = fake_sleep
        self.kwargs = kwargs

    async def __aenter__(self) -> Self:
        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        options: dict[str, Any] = {
            "base_url": BASE_URL,
            "transport": httpx.MockTransport(self._dispatch),
            "max_retries": 2,
            "retry_backoff_seconds": 0.0,
        }
        options.update(self.kwargs)
        if self.fake_sleep:
            options["sleep"] = sleep
        self.client = BeatAPIClient("test-api-key", **options)
        return self

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    async def __aexit__(self, *exc_info: object) -> None:
        await self.client.aclose()

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content.decode()) for request in self.requests]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def json_ok(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


SEARCH_PAYLOAD: dict[str, Any] = {
    "data": {
        "data": [
            {
                "reference": "data:tiktok.user.profile",
                "title": "TikTok user profile",
                "summary": "Public profile information",
                "price": "$0.002",
                "readiness": "ready",
                "signature": "username",
            },
            {
                "reference": "data:tiktok.user.posts",
                "title": "TikTok user posts",
                "summary": "Recent posts",
                "price": "$0.004",
                "readiness": "ready",
                "signature": "username, cursor?",
            },
        ],
        "recommended": {
            "reference": "data:tiktok.user.profile",
            "why_match": "best match for the query",
            "missing_inputs": ["username"],
        },
        "understood": {"terms": ["tiktok", "profile"]},
        "hints": [],
        "next_cursor": "",
        "next": {"action": "inspect", "call": "curl ..."},
    }
}

INSPECT_PAYLOAD: dict[str, Any] = {
    "data": {
        "reference": "data:tiktok.user.profile",
        "title": "TikTok user profile",
        "description": "Fetch a public TikTok profile.",
        "readiness": "ready",
        "input_schema": {
            "type": "object",
            "required": ["username"],
            "properties": {
                "username": {
                    "type": "string",
                    "minLength": 1,
                    "description": "TikTok handle without the @",
                },
                "fresh": {"type": "boolean", "description": "Bypass cache"},
            },
            "additionalProperties": False,
        },
        "pricing": {
            "price_usd": 0.002,
            "tiers": [
                {"name": "basic", "price_usd": 0.002},
                {"name": "full", "price_usd": 0.01},
            ],
        },
        "execution": {"mode": "async"},
        "schema_hash": "abc123",
        "next": {"action": "run", "note": "Send username"},
    }
}


# --------------------------------------------------------------------------- #
# Search / inspect
# --------------------------------------------------------------------------- #
def test_search_parses_cards_and_never_sends_the_key() -> None:
    async def scenario() -> tuple[Any, Harness]:
        async with Harness(lambda request: json_ok(SEARCH_PAYLOAD)) as harness:
            result = await harness.client.search("tiktok user profile", limit=3)
            return result, harness

    result, harness = run(scenario())

    assert len(result.cards) == 2
    assert result.cards[0].reference == "data:tiktok.user.profile"
    assert result.cards[0].price == "$0.002"
    assert result.recommended_reference == "data:tiktok.user.profile"
    assert result.missing_inputs == ("username",)
    assert result.terms == ("tiktok", "profile")
    assert result.pick() is not None and result.pick().reference == result.recommended_reference

    assert harness.paths == ["/v1/capabilities/search"]
    assert "authorization" not in {key.lower() for key in harness.requests[0].headers}
    assert harness.bodies()[0] == {
        "query": "tiktok user profile",
        "limit": 3,
        "view": "compact",
    }


def test_search_rejects_an_empty_query_without_calling_the_api() -> None:
    async def scenario() -> Harness:
        async with Harness(lambda request: json_ok({})) as harness:
            with pytest.raises(BeatAPIError) as excinfo:
                await harness.client.search("   ")
            assert excinfo.value.code == "empty_query"
            return harness

    harness = run(scenario())
    assert harness.requests == []


def test_search_without_results_keeps_hints_and_groups() -> None:
    payload = {
        "data": {
            "data": [],
            "hints": ["Try adding a platform"],
            "groups": [{"key": "search", "label": "Search", "count": 3, "examples": []}],
            "understood": {"terms": []},
        }
    }

    async def scenario() -> Any:
        async with Harness(lambda request: json_ok(payload)) as harness:
            return await harness.client.search("nothing here")

    result = run(scenario())
    assert result.cards == ()
    assert result.empty is False  # groups still make the response useful
    assert result.hints == ("Try adding a platform",)
    assert result.groups[0]["label"] == "Search"


def test_inspect_parses_contract_and_helpers() -> None:
    async def scenario() -> Any:
        async with Harness(lambda request: json_ok(INSPECT_PAYLOAD)) as harness:
            contract = await harness.client.inspect("data:tiktok.user.profile")
            assert harness.requests[0].url.path == "/v1/capabilities/inspect"
            assert "authorization" not in {key.lower() for key in harness.requests[0].headers}
            return contract

    contract = run(scenario())
    assert contract.reference == "data:tiktok.user.profile"
    assert contract.is_async is True
    assert contract.runnable is True
    assert contract.schema_hash == "abc123"
    assert contract.note == "Send username"
    assert contract.price_label == "$0.002"
    assert any("full" in tier for tier in contract.price_tiers())

    fields = describe_input_schema(contract.input_schema)
    assert [field.name for field in fields] == ["username", "fresh"]
    assert fields[0].required is True

    example = build_example_input(contract.input_schema)
    assert example == {"username": "text"}


def test_inspect_404_exposes_suggestions() -> None:
    payload = {
        "error": {
            "code": "not_found",
            "message": "Unknown reference",
            "retryable": False,
            "suggestions": ["data:tiktok.user.profile"],
            "request_id": "req_404",
        }
    }

    async def scenario() -> None:
        async with Harness(lambda request: json_ok(payload, 404)) as harness:
            with pytest.raises(BeatAPIError) as excinfo:
                await harness.client.inspect("data:tiktok.user.profil")
            error = excinfo.value
            assert error.status_code == 404
            assert error.retryable is False
            assert error.suggestions == ("data:tiktok.user.profile",)
            assert error.request_id == "req_404"
            assert len(harness.requests) == 1

    run(scenario())


def test_invented_reference_is_rejected_locally() -> None:
    async def scenario() -> None:
        async with Harness(lambda request: json_ok({})) as harness:
            with pytest.raises(BeatAPIError) as excinfo:
                await harness.client.run("tiktok:user", {"username": "nasa"})
            assert excinfo.value.code == "invalid_reference"
            assert harness.requests == []

    run(scenario())


# --------------------------------------------------------------------------- #
# Run / poll
# --------------------------------------------------------------------------- #
def test_run_sync_text_uses_bearer_auth_and_an_idempotency_key() -> None:
    payload = {
        "object": "text.result",
        "model": "jev-1",
        "status": "succeeded",
        "output_text": "سلام!",
        "request_id": "req_text",
    }

    async def scenario() -> Any:
        async with Harness(lambda request: json_ok(payload)) as harness:
            first = await harness.client.run("model:jev-1.13-free", {"input": "hi"})
            second = await harness.client.run("model:jev-1.13-free", {"input": "hi"})
            return first, second, harness

    first, second, harness = run(scenario())
    assert first.is_async is False
    assert first.output_text == "سلام!"
    assert first.request_id == "req_text"
    assert second.output_text == "سلام!"

    headers = harness.requests[0].headers
    assert headers["authorization"] == "Bearer test-api-key"
    bodies = harness.bodies()
    assert bodies[0]["operation"] == "start"
    assert bodies[0]["input"] == {"input": "hi"}
    assert bodies[0]["idempotency_key"] != bodies[1]["idempotency_key"]
    assert len(bodies[0]["idempotency_key"]) == 32


def test_run_async_then_poll_until_succeeded() -> None:
    task_payload = {
        "data": {
            "id": "task_123",
            "status": "queued",
            "task_kind": "image",
            "poll_after_seconds": 5,
        },
        "next": {"action": "status"},
    }
    status_payload = {
        "data": {
            "id": "task_123",
            "status": "succeeded",
            "output": {
                "media": [
                    {
                        "type": "image",
                        "url": "https://cdn.test/result.png",
                        "mime_type": "image/png",
                    }
                ],
                "r2_url": "https://cdn.test/result.png",
            },
            "usage": {"credits_charged": 0.02},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return json_ok(task_payload, 201)
        return json_ok(status_payload)

    async def scenario() -> tuple[Any, Any, Harness]:
        async with Harness(handler, rng=None) as harness:
            result = await harness.client.run("data:tiktok.user.profile", {"username": "nasa"})
            progress: list[str] = []

            async def on_progress(state: Any) -> None:
                progress.append(state.status)

            state = await harness.client.wait_for_task(
                result.task_id or "",
                reference=result.reference,
                on_progress=on_progress,
            )
            return result, state, harness

    result, state, harness = run(scenario())
    assert result.is_async is True
    assert result.task_id == "task_123"
    assert state.succeeded is True
    assert state.media[0].url == "https://cdn.test/result.png"
    assert state.media[0].kind == "image"
    assert result.payload["data"]["poll_after_seconds"] == 5
    assert harness.paths == ["/v1/capabilities/run", "/v1/tasks/task_123"]
    # GET /v1/tasks is authenticated as well, but only on api.beatapi.io.
    assert harness.requests[1].headers["authorization"] == "Bearer test-api-key"


def test_failed_task_raises_task_failed_error() -> None:
    start_payload = {
        "data": {"id": "task_ko", "status": "processing"},
        "next": {"action": "status"},
    }
    failed_payload = {
        "data": {
            "id": "task_ko",
            "status": "failed",
            "error_code": "content_policy_violation",
            "error_message": "The prompt was rejected",
            "retryable": False,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return json_ok(start_payload, 201) if request.method == "POST" else json_ok(failed_payload)

    async def scenario() -> None:
        async with Harness(handler) as harness:
            result = await harness.client.run("model:image-1", {"prompt": "x"})
            with pytest.raises(TaskFailedError) as excinfo:
                await harness.client.wait_for_task(result.task_id or "")
            error = excinfo.value
            assert error.code == "content_policy_violation"
            assert error.retryable is False
            assert error.task_id == "task_ko"

    run(scenario())


def test_polling_times_out_without_an_infinite_loop() -> None:
    start_payload = {
        "data": {"id": "task_slow", "status": "queued"},
        "next": {"action": "status"},
    }
    running_payload = {"data": {"id": "task_slow", "status": "processing"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return json_ok(start_payload, 201) if request.method == "POST" else json_ok(running_payload)

    async def scenario() -> int:
        async with Harness(
            handler,
            fake_sleep=False,
            poll_interval_min_seconds=0.005,
            poll_interval_max_seconds=0.01,
        ) as harness:
            result = await harness.client.run("model:video-1", {"prompt": "x"})
            with pytest.raises(TaskTimeoutError):
                await harness.client.wait_for_task(result.task_id or "", timeout_seconds=0.05)
            return len(harness.requests)

    assert run(scenario()) >= 2


# --------------------------------------------------------------------------- #
# Retry / error policy
# --------------------------------------------------------------------------- #
def test_429_respects_retry_after_header() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "7"},
                json={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "slow down",
                        "retryable": True,
                    }
                },
            )
        return json_ok(SEARCH_PAYLOAD)

    async def scenario() -> tuple[Any, Harness]:
        async with Harness(handler) as harness:
            result = await harness.client.search("tiktok")
            return result, harness

    result, harness = run(scenario())
    assert result.cards
    assert harness.sleeps == [7.0]
    assert len(harness.requests) == 2


def test_429_above_the_retry_cap_is_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "300"},
            json={
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "wait",
                    "retryable": True,
                }
            },
        )

    async def scenario() -> Harness:
        async with Harness(handler, max_retry_after_seconds=30) as harness:
            with pytest.raises(BeatAPIError) as excinfo:
                await harness.client.search("tiktok")
            assert excinfo.value.retry_after == 300.0
            return harness

    harness = run(scenario())
    assert len(harness.requests) == 1
    assert harness.sleeps == []


def test_non_retryable_error_is_never_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "bad_request",
                    "message": "Missing required input: keyword",
                    "retryable": False,
                }
            },
        )

    async def scenario() -> Harness:
        async with Harness(handler) as harness:
            with pytest.raises(BeatAPIError) as excinfo:
                await harness.client.run("data:xiaohongshu.app_v2.search_notes", {})
            assert excinfo.value.retryable is False
            assert excinfo.value.code == "bad_request"
            assert "keyword" in excinfo.value.message
            return harness

    harness = run(scenario())
    assert len(harness.requests) == 1
    assert harness.sleeps == []


def test_retryable_500_is_retried_once_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": "processing_unavailable",
                        "message": "try later",
                        "retryable": True,
                    }
                },
            )
        return json_ok(SEARCH_PAYLOAD)

    async def scenario() -> tuple[Any, Harness]:
        async with Harness(handler, max_retries=2) as harness:
            result = await harness.client.search("tiktok")
            return result, harness

    result, harness = run(scenario())
    assert result.cards
    assert len(harness.requests) == 2
    assert len(harness.sleeps) == 1


def test_retries_are_bounded() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": "internal_error",
                    "message": "down",
                    "retryable": True,
                }
            },
        )

    async def scenario() -> Harness:
        async with Harness(handler, max_retries=1) as harness:
            with pytest.raises(BeatAPIError) as excinfo:
                await harness.client.search("x")
            assert excinfo.value.retryable is True
            return harness

    harness = run(scenario())
    assert calls["count"] == 2
    assert len(harness.sleeps) == 1


def test_missing_key_is_rejected_before_any_request() -> None:
    with pytest.raises(ValueError):
        BeatAPIClient("   ")


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #
def test_download_returns_bytes_and_hides_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "image/png",
                "content-disposition": 'attachment; filename="preview.png"',
            },
            content=b"\x89PNG\r\n",
        )

    async def scenario() -> tuple[Any, Harness]:
        async with Harness(handler) as harness:
            downloaded = await harness.client.download("https://cdn.test/preview.png")
            return downloaded, harness

    downloaded, harness = run(scenario())
    assert downloaded.content == b"\x89PNG\r\n"
    assert downloaded.mime_type == "image/png"
    assert downloaded.filename == "preview.png"
    assert "authorization" not in {key.lower() for key in harness.requests[0].headers}


def test_download_refuses_files_above_the_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 64)

    async def scenario() -> None:
        async with Harness(handler) as harness:
            with pytest.raises(BeatAPIError) as excinfo:
                await harness.client.download("https://cdn.test/big.bin", max_bytes=8)
            assert excinfo.value.code == "file_too_large"

    run(scenario())


def test_download_rejects_non_http_urls() -> None:
    async def scenario() -> None:
        async with Harness(lambda request: json_ok({})) as harness:
            with pytest.raises(BeatAPIError):
                await harness.client.download("file:///etc/passwd")
            assert harness.requests == []

    run(scenario())


# --------------------------------------------------------------------------- #
# Input validation helpers
# --------------------------------------------------------------------------- #
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string", "minLength": 3, "maxLength": 1000},
        "width": {"type": "integer", "minimum": 256, "maximum": 2048},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "reference_image": {"type": "string", "format": "uri"},
    },
    "additionalProperties": False,
}


def test_validate_input_accepts_a_valid_payload() -> None:
    assert validate_input(SCHEMA, {"prompt": "a cat", "width": 512, "aspect_ratio": "16:9"}) == []


def test_validate_input_reports_missing_unknown_and_bad_types() -> None:
    errors = validate_input(SCHEMA, {"width": "big", "extra": 1, "aspect_ratio": "1:1"})
    joined = "\n".join(errors)
    assert "prompt" in joined
    assert "width" in joined
    assert "extra" in joined
    assert "aspect_ratio" in joined


def test_validate_input_checks_bounds_and_arrays() -> None:
    errors = validate_input(SCHEMA, {"prompt": "ok", "width": 10, "tags": ["a", "b", "c", "d"]})
    joined = "\n".join(errors)
    assert "input.prompt" in joined  # minLength = 3
    assert "input.width" in joined  # minimum = 256
    assert "input.tags" in joined  # maxItems = 3


def test_validate_input_rejects_non_object_payloads() -> None:
    assert validate_input(SCHEMA, [1, 2, 3])
    assert validate_input({}, {"anything": True}) == []


def test_parse_json_argument_strips_code_fences_and_rejects_arrays() -> None:
    assert parse_json_argument('```json\n{"prompt": "a cat"}\n```') == {"prompt": "a cat"}
    with pytest.raises(ValueError):
        parse_json_argument("[1, 2]")
    with pytest.raises(ValueError):
        parse_json_argument("")


# --------------------------------------------------------------------------- #
# Result rendering helpers
# --------------------------------------------------------------------------- #
def test_extract_media_handles_nested_output() -> None:
    payload = {
        "data": {
            "output": {
                "media": [
                    {
                        "type": "video",
                        "url": "https://cdn.test/clip.mp4",
                        "mime_type": "video/mp4",
                    },
                    {"type": "image", "url": "https://cdn.test/cover.jpg"},
                    {"type": "file", "url": "https://cdn.test/report.json"},
                ]
            }
        }
    }
    media = extract_media(payload)
    assert [item.kind for item in media] == ["video", "image", "file"]
    assert media[0].url.endswith("clip.mp4")


def test_extract_media_deduplicates_urls() -> None:
    payload = {
        "media": [
            {"url": "https://cdn.test/a.png"},
            {"r2_url": "https://cdn.test/a.png", "url": "https://cdn.test/a.png"},
        ]
    }
    assert len(extract_media(payload)) == 1


def test_extract_links_skips_next_instructions() -> None:
    payload = {
        "url": "https://example.com/page",
        "next": {"call": "https://api.beatapi.io/v1/capabilities/run"},
        "items": [{"link": "https://example.com/2"}],
    }
    links = extract_links(payload)
    assert links == ["https://example.com/page", "https://example.com/2"]


def test_extract_output_text_finds_nested_text() -> None:
    assert extract_output_text({"output_text": "hello"}) == "hello"
    assert extract_output_text({"data": {"output": {"markdown": "# report"}}}) == "# report"
    assert extract_output_text({"items": [{"title": "x"}]}) == ""


def test_format_price_handles_all_shapes() -> None:
    assert format_price(0.002) == "$0.002"
    assert format_price("$1 from") == "$1 from"
    assert format_price({"price_usd": 0.15}) == "$0.15"
    assert format_price({"tiers": [{"price_usd": 0.15}, {"price_usd": 1.85}]}) == "$0.15 / $1.85"
    assert format_price(None) == ""


def test_chunk_text_stays_under_the_limit() -> None:
    text = ("line of text\n" * 500).strip()
    chunks = chunk_text(text, 1000)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_rate_limiter_counts_requests_in_a_window() -> None:
    now = {"value": 0.0}
    limiter = UserRateLimiter(2, window_seconds=60, clock=lambda: now["value"])
    assert limiter.allow(42) is True
    assert limiter.allow(42) is True
    assert limiter.allow(42) is False
    now["value"] = 61.0
    assert limiter.allow(42) is True


def test_secret_filter_redacts_tokens_in_log_records() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Authorization: Bearer %s",
        args=("super-secret-token",),
        exc_info=None,
    )
    SecretRedactingFilter(["super-secret-token"]).filter(record)
    assert "super-secret-token" not in record.getMessage()
    assert "***" in record.getMessage()


# --------------------------------------------------------------------------- #
# Configuration and webhook hardening
# --------------------------------------------------------------------------- #
def test_settings_normalise_the_webhook_url_and_parse_user_ids() -> None:
    settings = Settings(
        telegram_bot_token="123456:fake-token",
        beatapi_api_key="test-key",
        mode="webhook",
        webhook_url="https://bot.example.com/",
        allowed_user_ids="1, 2  3",
    )
    assert settings.webhook_url == "https://bot.example.com/webhook"
    assert settings.webhook_path == "/webhook"
    assert settings.allowed_user_id_set == frozenset({1, 2, 3})
    assert settings.webhook_secret
    assert "123456:fake-token" not in settings.webhook_secret
    assert "test-key" not in repr(settings)


def test_webhook_mode_requires_a_public_url() -> None:
    with pytest.raises(ValidationError):
        Settings(
            telegram_bot_token="123456:fake-token",
            beatapi_api_key="test-key",
            mode="webhook",
            webhook_url="",
        )

    polling = Settings(
        telegram_bot_token="123456:fake-token",
        beatapi_api_key="test-key",
        mode="polling",
    )
    assert polling.webhook_url == ""


def test_webhook_endpoint_rejects_a_wrong_secret_token() -> None:
    settings = Settings(
        telegram_bot_token="123456:fake-token",
        beatapi_api_key="test-key",
        mode="webhook",
        webhook_url="https://bot.example.com/webhook",
        webhook_secret_token="top-secret",
    )
    app = create_app(settings)

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            rejected = await client.post("/webhook", json={"update_id": 1})
            # Lifespan was not started, so the app reports itself as not ready.
            health = await client.get("/healthz")
            index = await client.get("/")
            return rejected, health, index

    rejected, health, index = run(scenario())
    assert rejected.status_code == 401
    assert health.status_code == 503
    assert index.json()["mode"] == "webhook"


class RecordingBot:
    """Captures the Telegram calls made by ``deliver_payload``."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.documents: list[tuple[Any, str | None]] = []
        self.photos: list[str | None] = []
        self.videos: list[str | None] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.messages.append(text)

    async def send_document(self, chat_id: int, document: Any, filename: str | None = None, **kwargs: Any) -> None:
        self.documents.append((document, filename))

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> None:
        self.photos.append(getattr(photo, "name", None))

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> None:
        self.videos.append(getattr(video, "name", None))


class StubDownloader:
    def __init__(self, content: bytes = b"binary", mime_type: str = "image/png") -> None:
        self.calls: list[str] = []
        self.content = content
        self.mime_type = mime_type

    async def download(self, url: str, *, max_bytes: int | None = None) -> DownloadedFile:
        self.calls.append(url)
        return DownloadedFile(content=self.content, mime_type=self.mime_type, filename="file.bin")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "telegram_bot_token": "123456789:" + "A" * 35,
        "beatapi_api_key": "test-key",
        "mode": "polling",
    }
    values.update(overrides)
    return Settings(**values)


def test_deliver_payload_sends_output_text() -> None:
    bot = RecordingBot()
    payload = {"output_text": "نتیجه‌ی متنی"}

    run(deliver_payload(bot, StubDownloader(), 7, payload, title="t", settings=make_settings()))

    assert bot.messages == ["نتیجه‌ی متنی"]


def test_deliver_payload_downloads_and_sends_media() -> None:
    bot = RecordingBot()
    downloader = StubDownloader()
    payload = {
        "output": {
            "media": [
                {
                    "type": "image",
                    "url": "https://cdn.test/a.png",
                    "mime_type": "image/png",
                }
            ]
        }
    }

    run(deliver_payload(bot, downloader, 7, payload, title="t", settings=make_settings()))

    assert downloader.calls == ["https://cdn.test/a.png"]
    assert bot.photos == ["file.bin"]


def test_deliver_payload_falls_back_to_json() -> None:
    bot = RecordingBot()
    payload = {"unexpected": {"shape": [1, 2, 3]}}

    run(deliver_payload(bot, StubDownloader(), 7, payload, title="t", settings=make_settings()))

    assert bot.messages
    assert "<pre>" in bot.messages[0]


def test_webhook_accepts_an_authenticated_update() -> None:
    settings = make_settings(
        mode="webhook",
        webhook_url="https://bot.example.com/webhook",
        webhook_secret_token="top-secret",
    )
    fastapi_app = create_app(settings)
    telegram_app = Application.builder().token(settings.telegram_token).updater(None).build()
    fastapi_app.state.telegram_app = telegram_app

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fastapi_app), base_url="http://testserver"
        ) as client:
            accepted = await client.post(
                "/webhook",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": "top-secret"},
            )
            health = await client.get("/healthz")
            return accepted, health

    accepted, health = run(scenario())
    assert accepted.status_code == 200
    assert telegram_app.update_queue.qsize() == 1
    assert health.status_code == 200


# --------------------------------------------------------------------------- #
# End-to-end handler flow (real client + mocked transport + fake bot)
# --------------------------------------------------------------------------- #
class FakeMessage:
    """Minimal stand-in for ``telegram.Message`` used by the handlers."""

    def __init__(
        self,
        text: str = "",
        chat_id: int = 42,
        log: list[tuple[str, str]] | None = None,
    ):
        self.text = text
        self.chat_id = chat_id
        self.log = log if log is not None else []

    async def reply_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.log.append(("reply", text))
        return FakeMessage(text=text, chat_id=self.chat_id, log=self.log)

    async def edit_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self.log.append(("edit", text))
        return self


class FakeBot:
    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.messages: list[str] = []
        self.photos: list[Any] = []
        self.videos: list[Any] = []
        self.audio: list[Any] = []
        self.documents: list[Any] = []
        self.actions: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> FakeMessage:
        self.messages.append(text)
        return FakeMessage(text=text, chat_id=chat_id, log=self.log)

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> None:
        self.photos.append(photo)

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> None:
        self.videos.append(video)

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> None:
        self.audio.append(audio)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> None:
        self.documents.append(document)

    async def send_chat_action(self, chat_id: int, action: str, **kwargs: Any) -> None:
        self.actions.append(action)


def make_context(bot: FakeBot, client: BeatAPIClient) -> SimpleNamespace:
    settings = make_settings()
    application = SimpleNamespace(
        bot_data={
            "settings": settings,
            "beatapi": client,
            "rate_limiter": UserRateLimiter(0),
        }
    )
    return SimpleNamespace(application=application, bot=bot, bot_data=application.bot_data, user_data={})


def test_search_then_run_sync_flow_end_to_end() -> None:
    sync_result = {
        "object": "text.result",
        "status": "succeeded",
        "output_text": "نتیجه‌ی نهایی برای nasa",
        "request_id": "req_1",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return json_ok(SEARCH_PAYLOAD)
        if path.endswith("/inspect"):
            return json_ok(INSPECT_PAYLOAD)
        if path.endswith("/run"):
            return json_ok(sync_result)
        raise AssertionError(f"unexpected call to {path}")

    async def scenario() -> tuple[SimpleNamespace, FakeBot]:
        bot = FakeBot()
        client = BeatAPIClient(
            "test-api-key",
            base_url=BASE_URL,
            transport=httpx.MockTransport(handler),
        )
        context = make_context(bot, client)
        try:
            await search_command(
                SimpleNamespace(effective_message=FakeMessage(text="/search tiktok user profile", log=bot.log)),
                context,
            )
            assert "session" in context.user_data
            await run_command(
                SimpleNamespace(effective_message=FakeMessage(text='/run {"username": "nasa"}', log=bot.log)),
                context,
            )
            return context, bot
        finally:
            await client.aclose()

    _context, bot = run(scenario())

    rendered = [text for kind, text in bot.log if kind in {"reply", "edit"}]
    assert any("TikTok user profile" in text for text in rendered)
    assert any("data:tiktok.user.profile" in text for text in rendered)
    assert any("$0.002" in text for text in rendered)
    assert bot.messages == ["نتیجه‌ی نهایی برای nasa"]


def test_run_rejects_input_that_does_not_match_the_schema() -> None:
    async def scenario() -> tuple[SimpleNamespace, FakeBot, list[str]]:
        bot = FakeBot()
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/search"):
                return json_ok(SEARCH_PAYLOAD)
            if request.url.path.endswith("/inspect"):
                return json_ok(INSPECT_PAYLOAD)
            return json_ok({"output_text": "should not run"})

        client = BeatAPIClient("test-api-key", base_url=BASE_URL, transport=httpx.MockTransport(handler))
        context = make_context(bot, client)
        try:
            await search_command(
                SimpleNamespace(effective_message=FakeMessage(text="/search tiktok user profile", log=bot.log)),
                context,
            )
            await run_command(
                SimpleNamespace(effective_message=FakeMessage(text='/run {"unknown": 1}', log=bot.log)),
                context,
            )
            return context, bot, paths
        finally:
            await client.aclose()

    _context, bot, paths = run(scenario())
    assert "/v1/capabilities/run" not in paths  # rejected before spending balance
    assert any("سازگار نیست" in text for kind, text in bot.log if kind == "reply")
    assert bot.messages == []


def test_async_task_flow_delivers_the_downloaded_file() -> None:
    task_start = {"data": {"id": "task_x", "status": "queued", "task_kind": "image"}}
    task_done = {
        "data": {
            "id": "task_x",
            "status": "succeeded",
            "output": {
                "media": [
                    {
                        "type": "image",
                        "url": "https://cdn.test/result.png",
                        "mime_type": "image/png",
                    }
                ]
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return json_ok(SEARCH_PAYLOAD)
        if path.endswith("/inspect"):
            return json_ok(INSPECT_PAYLOAD)
        if path.endswith("/run"):
            return json_ok(task_start, 201)
        if path.startswith("/v1/tasks/"):
            return json_ok(task_done)
        if path == "/result.png":
            return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})
        raise AssertionError(f"unexpected call to {path}")

    async def scenario() -> tuple[SimpleNamespace, FakeBot]:
        bot = FakeBot()
        client = BeatAPIClient(
            "test-api-key",
            base_url=BASE_URL,
            transport=httpx.MockTransport(handler),
            sleep=lambda seconds: asyncio.sleep(0),
        )
        context = make_context(bot, client)
        try:
            await search_command(
                SimpleNamespace(effective_message=FakeMessage(text="/search tiktok user profile", log=bot.log)),
                context,
            )
            await run_command(
                SimpleNamespace(effective_message=FakeMessage(text='/run {"username": "nasa"}', log=bot.log)),
                context,
            )
            return context, bot
        finally:
            await client.aclose()

    _context, bot = run(scenario())

    assert len(bot.photos) == 1
    assert getattr(bot.photos[0], "name", "") == "result.png"
    assert any("تکمیل شد" in text for kind, text in bot.log if kind == "edit")


def test_formatter_redacts_secrets_from_tracebacks() -> None:
    formatter = RedactingFormatter("%(message)s", "%Y-%m-%d", ["super-secret-token"])
    try:
        raise RuntimeError("leaked super-secret-token in a traceback")
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="boom",
            args=(),
            exc_info=sys.exc_info(),
        )
    output = formatter.format(record)
    assert "super-secret-token" not in output
    assert "***" in output
