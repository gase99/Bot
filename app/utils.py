"""Small, dependency-free helpers: formatting, media parsing, validation and rendering.

Nothing in this module imports other application modules, so it can be used by
the API client, the Telegram handlers and the tests without circular imports.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from collections import deque
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:  # annotations only; avoids a circular import at runtime
    from app.beatapi import (
        BeatAPIError,
        CapabilityCard,
        CapabilityContract,
        TaskFailedError,
        TaskTimeoutError,
    )

LOGGER = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE: Final = 3800
MAX_DEPTH: Final = 6
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

_MIME_EXTENSIONS: Final[dict[str, str]] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/markdown": ".md",
}
_EXTENSION_KINDS: Final[dict[str, str]] = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".gif": "image",
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".m4v": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".ogg": "audio",
    ".opus": "audio",
}


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def html_escape(value: object) -> str:
    """Escape a value for Telegram's HTML parse mode."""
    return html.escape(str(value), quote=False)


def truncate(value: object, limit: int, *, suffix: str = "…") -> str:
    """Collapse whitespace and cut a value to ``limit`` characters."""
    text = " ".join(str(value).split()) if value is not None else ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def chunk_text(text: str, limit: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    """Split text into chunks that fit a Telegram message without breaking words."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n")
    return chunks


# --------------------------------------------------------------------------- #
# Pricing / status formatting
# --------------------------------------------------------------------------- #
def _format_usd(amount: float) -> str:
    if amount == 0:
        return "$0"
    if abs(amount) < 0.01:
        return f"${amount:.6f}".rstrip("0").rstrip(".")
    return f"${amount:,.4f}".rstrip("0").rstrip(".")


_PRICE_KEYS: Final = (
    "display",
    "label",
    "price_usd",
    "price",
    "amount_usd",
    "amount",
    "from_usd",
    "min_usd",
    "starting_at_usd",
    "usd",
)


def format_price(value: Any) -> str:
    """Render any of the price shapes BeatAPI may return as a short string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return _format_usd(float(value))
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in _PRICE_KEYS:
            candidate = value.get(key)
            if candidate in (None, "", [], {}):
                continue
            rendered = format_price(candidate)
            if rendered:
                return rendered
        tiers = value.get("tiers")
        if isinstance(tiers, Sequence) and not isinstance(tiers, (str, bytes)) and tiers:
            rendered_tiers = [format_price(tier) for tier in tiers]
            rendered_tiers = [item for item in rendered_tiers if item]
            if rendered_tiers:
                return " / ".join(rendered_tiers[:3])
        return ""
    return ""


def format_price_tiers(pricing: Any, limit: int = 4) -> list[str]:
    """Return readable ``name — price`` lines when the contract publishes tiers."""
    if not isinstance(pricing, Mapping):
        return []
    tiers = pricing.get("tiers")
    if not isinstance(tiers, Sequence) or isinstance(tiers, (str, bytes)):
        return []

    lines: list[str] = []
    for tier in list(tiers)[:limit]:
        if isinstance(tier, Mapping):
            name = str(tier.get("name") or tier.get("label") or tier.get("shape") or "").strip()
            price = format_price(tier)
            if name and price:
                lines.append(f"{name}: {price}")
            elif price:
                lines.append(price)
        else:
            price = format_price(tier)
            if price:
                lines.append(price)
    return lines


def format_duration(seconds: float) -> str:
    """Human readable duration in Persian, e.g. ``2 دقیقه و 5 ثانیه``."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} ثانیه"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} دقیقه" + (f" و {secs} ثانیه" if secs else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ساعت و {minutes} دقیقه"


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
_FENCE_START = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*")
_FENCE_END = re.compile(r"\s*```\s*$")


def parse_json_argument(raw: str) -> dict[str, Any]:
    """Parse the JSON a user passed to ``/run``, tolerating code fences.

    Raises:
        ValueError: when the payload is not a JSON object.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("ورودی JSON خالی است.")
    text = _FENCE_START.sub("", text)
    text = _FENCE_END.sub("", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON نامعتبر است: خط {exc.lineno}، ستون {exc.colno} ({exc.msg}).") from exc
    if not isinstance(parsed, dict):
        raise ValueError(  # noqa: TRY004 - callers catch ValueError for bad user input
            'ورودی باید یک شیء JSON باشد، مثلاً {"prompt": "..."}.'
        )
    return parsed


def format_json(payload: Any, max_chars: int = DEFAULT_CHUNK_SIZE) -> str:
    """Pretty-print a payload, cut to ``max_chars``."""
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = str(payload)
    if len(text) > max_chars:
        return text[: max(0, max_chars - 1)] + "…"
    return text


# --------------------------------------------------------------------------- #
# Input schema helpers
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class FieldSpec:
    """One row of a capability's ``input_schema``."""

    name: str
    type: str
    required: bool
    description: str = ""
    enum: tuple[str, ...] = ()
    has_default: bool = False
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None


def _type_label(prop: Mapping[str, Any]) -> str:
    declared = prop.get("type")
    if isinstance(declared, str):
        label = declared
    elif isinstance(declared, list):
        label = " | ".join(str(item) for item in declared)
    else:
        label = ""
        for key in ("anyOf", "oneOf", "allOf"):
            options = prop.get(key)
            if isinstance(options, list):
                labels = [_type_label(option) for option in options if isinstance(option, Mapping)]
                labels = [item for item in labels if item]
                if labels:
                    label = " | ".join(dict.fromkeys(labels))
                    break
        if not label and isinstance(prop.get("$ref"), str):
            label = str(prop["$ref"]).rsplit("/", 1)[-1]
    if not label:
        label = "any"
    if prop.get("format"):
        label = f"{label} ({prop['format']})"
    return label


def describe_input_schema(schema: Any, limit: int = 16) -> list[FieldSpec]:
    """Turn a JSON Schema object into displayable field rows (required first)."""
    if not isinstance(schema, Mapping):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return []

    required_raw = schema.get("required")
    required = {str(item) for item in required_raw} if isinstance(required_raw, list) else set()
    for name, prop in properties.items():
        if isinstance(prop, Mapping) and prop.get("required") is True:
            required.add(str(name))

    fields: list[FieldSpec] = []
    for name, prop in properties.items():
        spec = prop if isinstance(prop, Mapping) else {}
        enum_raw = spec.get("enum")
        enum = tuple(str(item) for item in enum_raw) if isinstance(enum_raw, list) else ()
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        fields.append(
            FieldSpec(
                name=str(name),
                type=_type_label(spec),
                required=str(name) in required,
                description=truncate(spec.get("description", ""), 140),
                enum=enum,
                has_default="default" in spec,
                default=spec.get("default"),
                minimum=float(minimum) if isinstance(minimum, (int, float)) else None,
                maximum=float(maximum) if isinstance(maximum, (int, float)) else None,
            )
        )
    fields.sort(key=lambda field: (not field.required, field.name))
    return fields[:limit]


def build_example_input(schema: Any, *, required_only: bool = True, depth: int = 0) -> dict[str, Any]:
    """Build a copy-pasteable example input for ``/run``."""
    if not isinstance(schema, Mapping) or depth > MAX_DEPTH:
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    required_raw = schema.get("required")
    required = {str(item) for item in required_raw} if isinstance(required_raw, list) else set()

    example: dict[str, Any] = {}
    for name, prop in properties.items():
        if required_only and required and name not in required:
            continue
        if not isinstance(prop, Mapping):
            continue
        example[str(name)] = _example_value(prop, depth=depth)
    return example


def _example_value(prop: Mapping[str, Any], *, depth: int) -> Any:
    if "default" in prop and prop["default"] not in (None, ""):
        return prop["default"]
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    declared = prop.get("type")
    if declared == "object":
        return build_example_input(prop, depth=depth + 1)
    if declared == "array":
        items = prop.get("items")
        return [_example_value(items, depth=depth + 1)] if isinstance(items, Mapping) else []
    if declared in {"integer", "number"}:
        minimum = prop.get("minimum")
        return minimum if isinstance(minimum, (int, float)) else 1
    if declared == "boolean":
        return False
    if declared == "string":
        fmt = str(prop.get("format") or "")
        if fmt in {"uri", "url", "uri-reference"}:
            return "https://example.com/file.jpg"
        return "text"
    return "text"


def validate_input(schema: Any, value: Any) -> list[str]:
    """Validate a user-supplied input against a capability's JSON Schema.

    Returns a list of human readable (Persian) errors; empty means valid.
    """
    if not isinstance(value, Mapping):
        return ['ورودی باید یک شیء JSON باشد، مثلاً {"prompt": "..."}.']
    if not isinstance(schema, Mapping) or not schema:
        return []
    errors: list[str] = []
    _validate_object(schema, value, "input", errors, depth=0)
    return errors


def _validate_object(
    schema: Mapping[str, Any],
    value: Mapping[str, Any],
    path: str,
    errors: list[str],
    *,
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        return
    properties_raw = schema.get("properties")
    properties = properties_raw if isinstance(properties_raw, Mapping) else {}

    required_raw = schema.get("required")
    required = {str(item) for item in required_raw} if isinstance(required_raw, list) else set()
    for name, prop in properties.items():
        if isinstance(prop, Mapping) and prop.get("required") is True:
            required.add(str(name))
    for name in sorted(required):
        if name not in value:
            errors.append(f"فیلد «{name}» الزامی است.")

    additional = schema.get("additionalProperties")
    allow_extra = additional is True
    if properties:
        for key, item in value.items():
            spec = properties.get(key)
            if isinstance(spec, Mapping):
                _validate_value(spec, item, f"{path}.{key}", errors, depth=depth + 1)
            elif not allow_extra:
                allowed = "، ".join(list(properties)[:8]) or "—"
                errors.append(f"فیلد ناشناخته «{key}» مجاز نیست. فیلدهای مجاز: {allowed}")


def _validate_value(
    spec: Mapping[str, Any],
    value: Any,
    path: str,
    errors: list[str],
    *,
    depth: int,
) -> None:
    if depth > MAX_DEPTH:
        return

    for key in ("anyOf", "oneOf"):
        options = spec.get(key)
        if isinstance(options, list) and options:
            branch_errors: list[list[str]] = []
            for option in options:
                if not isinstance(option, Mapping):
                    continue
                trial: list[str] = []
                _validate_value(option, value, path, trial, depth=depth + 1)
                if not trial:
                    return
                branch_errors.append(trial)
            if branch_errors:
                errors.append(f"مقدار «{path}» با هیچ‌یک از قالب‌های مجاز سازگار نیست.")
                return

    declared = spec.get("type")
    if isinstance(declared, str) and not _matches_type(declared, value):
        errors.append(f"نوع «{path}» باید {declared} باشد (مقدار داده‌شده: {_type_name(value)}).")
        return
    if isinstance(declared, list) and declared and not any(_matches_type(str(item), value) for item in declared):
        allowed = " یا ".join(str(item) for item in declared)
        errors.append(f"نوع «{path}» باید {allowed} باشد (مقدار داده‌شده: {_type_name(value)}).")
        return

    enum = spec.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        allowed = "، ".join(str(item) for item in enum[:10])
        errors.append(f"مقدار «{path}» باید یکی از این‌ها باشد: {allowed}")

    if isinstance(value, str):
        min_length = spec.get("minLength")
        max_length = spec.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"طول «{path}» باید حداقل {min_length} نویسه باشد.")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"طول «{path}» باید حداکثر {max_length} نویسه باشد.")
        pattern = spec.get("pattern")
        if isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                matched = True
            if not matched:
                errors.append(f"مقدار «{path}» با الگوی موردنیاز سازگار نیست.")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"مقدار «{path}» باید بزرگ‌تر یا مساوی {minimum} باشد.")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"مقدار «{path}» باید کوچک‌تر یا مساوی {maximum} باشد.")

    if isinstance(value, list):
        min_items = spec.get("minItems")
        max_items = spec.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"«{path}» باید حداقل {min_items} عضو داشته باشد.")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"«{path}» باید حداکثر {max_items} عضو داشته باشد.")
        items = spec.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value[:50]):
                _validate_value(items, item, f"{path}[{index}]", errors, depth=depth + 1)

    if isinstance(value, Mapping):
        _validate_object(spec, value, path, errors, depth=depth)


def _matches_type(declared: str, value: Any) -> bool:
    if declared == "object":
        return isinstance(value, Mapping)
    if declared == "array":
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "null":
        return value is None
    return True


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence):
        return "array"
    return type(value).__name__


# --------------------------------------------------------------------------- #
# Media / result parsing
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class MediaFile:
    """A media URL found inside a BeatAPI result."""

    url: str
    kind: str = "file"  # image | video | audio | file
    mime_type: str = ""
    filename: str = ""


_URL_KEYS: Final = ("url", "r2_url", "download_url", "file_url")
_LINK_KEYS: Final = ("url", "link", "source_url", "html_url", "permalink", "post_url", "web_url")
_TEXT_KEYS: Final = ("output_text", "text", "markdown", "answer", "content", "summary")
_MEDIA_CONTAINERS: Final = ("media", "files", "attachments", "images", "videos")
_RESULT_CONTAINERS: Final = ("output", "data", "result", "results", "items")


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def url_basename(url: str) -> str:
    """Last path segment of a URL, decoded and without a query string."""
    try:
        path = urlparse(str(url)).path
    except ValueError:
        return ""
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


def safe_filename(name: str, fallback: str = "result") -> str:
    """Restrict a filename to a safe, short, extension-preserving token."""
    candidate = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    candidate = _SAFE_FILENAME.sub("_", candidate).strip("._")
    if not candidate:
        candidate = fallback
    if len(candidate) > 90:
        stem, dot, extension = candidate.rpartition(".")
        if dot and len(extension) <= 8:
            candidate = stem[: 90 - len(extension) - 1] + "." + extension
        else:
            candidate = candidate[:90]
    return candidate or fallback


def extension_for(mime_type: str = "", kind: str = "file") -> str:
    """Best-effort file extension for a MIME type."""
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[mime]
    return {
        "image": ".jpg",
        "video": ".mp4",
        "audio": ".mp3",
        "file": ".bin",
    }.get(kind, ".bin")


def guess_media_kind(*, mime_type: str = "", url: str = "", declared: str = "") -> str:
    """Classify a media entry as image, video, audio or file."""
    kind = (declared or "").strip().lower()
    if kind in {"photo", "image", "picture"}:
        return "image"
    if kind in {"video", "movie", "clip"}:
        return "video"
    if kind in {"audio", "voice", "music"}:
        return "audio"
    if kind in {"file", "document", "archive"}:
        return "file"

    mime = (mime_type or "").split(";")[0].strip().lower()
    for prefix, mapped in (("image/", "image"), ("video/", "video"), ("audio/", "audio")):
        if mime.startswith(prefix):
            return mapped

    extension = ("." + url_basename(url).rsplit(".", 1)[-1].lower()) if "." in url_basename(url) else ""
    return _EXTENSION_KINDS.get(extension, "file")


def _first_url(entry: Mapping[str, Any]) -> str:
    for key in _URL_KEYS:
        candidate = entry.get(key)
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    return ""


def extract_media(payload: Any, limit: int = 10) -> list[MediaFile]:
    """Collect hosted media files from a BeatAPI result or task payload."""
    found: list[MediaFile] = []
    seen: set[str] = set()

    def add(entry: Mapping[str, Any]) -> None:
        if len(found) >= limit:
            return
        url = _first_url(entry)
        if not url or url in seen:
            return
        declared = str(entry.get("type") or entry.get("media_type") or "")
        mime = str(entry.get("mime_type") or entry.get("content_type") or "")
        if mime.lower() in {"image", "video", "audio", "file"}:
            mime = ""
        name = str(entry.get("filename") or entry.get("name") or entry.get("title") or "")
        kind = guess_media_kind(mime_type=mime, url=url, declared=declared)
        seen.add(url)
        found.append(
            MediaFile(
                url=url,
                kind=kind,
                mime_type=mime,
                filename=safe_filename(name or url_basename(url) or "result"),
            )
        )

    def visit(node: Any, depth: int) -> None:
        if depth > MAX_DEPTH or len(found) >= limit:
            return
        if isinstance(node, Mapping):
            for key in _MEDIA_CONTAINERS:
                container = node.get(key)
                if _is_sequence(container):
                    for entry in container:
                        if isinstance(entry, Mapping):
                            add(entry)
            for key in _RESULT_CONTAINERS:
                if key in node:
                    visit(node[key], depth + 1)
        elif _is_sequence(node):
            for item in node:
                visit(item, depth + 1)

    visit(payload, 0)
    return found


def extract_links(payload: Any, limit: int = 8, exclude: Iterable[str] = ()) -> list[str]:
    """Collect unique http(s) links from a result, skipping ``next`` instructions."""
    skipped = set(exclude)
    links: list[str] = []
    seen: set[str] = set()

    def visit(node: Any, depth: int) -> None:
        if depth > MAX_DEPTH or len(links) >= limit:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "next":
                    continue
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    if key in _LINK_KEYS and value not in seen and value not in skipped:
                        seen.add(value)
                        links.append(value)
                elif isinstance(value, (Mapping, list)):
                    visit(value, depth + 1)
        elif _is_sequence(node):
            for item in node:
                visit(item, depth + 1)

    visit(payload, 0)
    return links[:limit]


def extract_output_text(payload: Any) -> str:
    """Find the human readable text of a result (text models, research, ...)."""
    if not isinstance(payload, Mapping):
        return ""
    for key in ("output_text", "text", "answer", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for container_key in _RESULT_CONTAINERS:
        container = payload.get(container_key)
        if isinstance(container, str) and container.strip():
            return container.strip()
        if isinstance(container, Mapping):
            for key in _TEXT_KEYS:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            nested = container.get("output") or container.get("result")
            if isinstance(nested, Mapping):
                for key in _TEXT_KEYS:
                    value = nested.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
class UserRateLimiter:
    """Tiny in-memory sliding-window limiter keyed by Telegram user id."""

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        clock: Any = time.monotonic,
    ) -> None:
        self._limit = max(0, limit)
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}

    def allow(self, key: Hashable) -> bool:
        """Return ``True`` when the caller may proceed, recording the hit."""
        if self._limit == 0:
            return True
        now = float(self._clock())
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    def retry_after(self, key: Hashable) -> float:
        """Seconds until the next request would be allowed."""
        hits = self._hits.get(key)
        if not hits:
            return 0.0
        return max(0.0, self._window - (float(self._clock()) - hits[0]))

    def clear(self) -> None:
        self._hits.clear()


def jitter_delay(minimum: float, maximum: float, rng: random.Random | None = None) -> float:
    """Random delay inside ``[minimum, maximum]`` used between task polls."""
    generator = rng or random
    low, high = sorted((max(0.0, minimum), max(0.0, maximum)))
    return generator.uniform(low, high)


# Result rendering (pure functions, no Telegram dependency)

MAX_ITEM_LINES: Final = 12

READINESS_LABELS: Final[dict[str, str]] = {
    "ready": "آماده اجرا",
    "runnable": "قابل اجرا",
    "listed": "غیرقابل اجرا (فقط فهرست‌شده)",
    "": "نامشخص",
}


def readiness_label(value: str) -> str:
    """Translate BeatAPI's ``readiness`` value into Persian."""
    return READINESS_LABELS.get((value or "").lower(), value or "نامشخص")


def render_contract(contract: CapabilityContract) -> str:
    """Render an inspected capability as an HTML message."""
    lines: list[str] = [f"🎯 <b>{html_escape(contract.title)}</b>"]
    lines.append(f"<code>{html_escape(contract.reference)}</code>")
    if contract.description:
        lines += ["", html_escape(truncate(contract.description, 600))]

    lines += ["", f"💵 قیمت: <b>{html_escape(contract.price_label)}</b>"]
    tiers = contract.price_tiers()
    if tiers:
        lines.append("   " + " · ".join(html_escape(tier) for tier in tiers))
    mode = "غیرهمگام (async)" if contract.is_async else "همگام (sync)"
    lines.append(f"⚙️ اجرا: {mode} — وضعیت: {html_escape(readiness_label(contract.readiness))}")

    fields = describe_input_schema(contract.input_schema)
    if fields:
        required = [item for item in fields if item.required]
        optional = [item for item in fields if not item.required]
        if required:
            lines += ["", "📥 <b>ورودی‌های الزامی:</b>"]
            for spec in required[:8]:
                detail = f" — {html_escape(spec.description)}" if spec.description else ""
                lines.append(f" • <code>{html_escape(spec.name)}</code> ({html_escape(spec.type)}){detail}")
            if len(required) > 8:
                lines.append(f" … و {len(required) - 8} فیلد الزامی دیگر")
        if optional:
            names = "، ".join(f"<code>{html_escape(item.name)}</code>" for item in optional[:8])
            lines += ["", f"📎 اختیاری: {names}"]
            if len(optional) > 8:
                lines.append(f" … و {len(optional) - 8} فیلد دیگر")
    else:
        lines += ["", "📥 این قابلیت ورودی خاصی ندارد."]

    example = build_example_input(contract.input_schema)
    example_text = json.dumps(example, ensure_ascii=False) if example else "{}"
    lines += ["", "▶️ اجرا:", f"<code>/run {html_escape(example_text)}</code>"]
    if contract.note:
        lines += ["", f"ℹ️ {html_escape(truncate(contract.note, 200))}"]
    return "\n".join(lines)


def render_search_empty(
    query: str, hints: tuple[str, ...], groups: tuple[Mapping[str, Any], ...], terms: tuple[str, ...]
) -> str:
    """Explain a search with no cards, using hints and groups."""
    lines = [f"🔍 نتیجه‌ای برای «{html_escape(truncate(query, 120))}» پیدا نشد."]
    if terms:
        lines += ["", "کلمات شناسایی‌شده: " + "، ".join(html_escape(term) for term in terms[:8])]
    if hints:
        lines += ["", "💡 پیشنهادها:"]
        lines += [f"• {html_escape(truncate(hint, 160))}" for hint in hints[:5]]
    if groups:
        lines += ["", "📚 این پلتفرم چه امکاناتی دارد:"]
        for group in groups[:6]:
            label = group.get("label") or group.get("key") or ""
            count = group.get("count")
            examples = group.get("examples") if isinstance(group.get("examples"), list) else []
            sample = ""
            if examples and isinstance(examples[0], Mapping):
                sample = str(examples[0].get("reference") or "")
            suffix = f" ({count})" if isinstance(count, int) else ""
            sample_text = f" — <code>{html_escape(sample)}</code>" if sample else ""
            lines.append(f"• <b>{html_escape(label)}</b>{suffix}{sample_text}")
    lines += ["", "می‌توانید عبارت را دقیق‌تر بنویسید؛ مثلاً: <code>/search tiktok user profile</code>"]
    return "\n".join(lines)


def render_api_error(error: BeatAPIError) -> str:
    """Render a sanitised BeatAPI error with a helpful hint."""
    titles = {
        401: "کلید BeatAPI پذیرفته نشد",
        402: "اعتبار حساب BeatAPI کافی نیست",
        403: "دسترسی به این قابلیت مجاز نیست",
        404: "قابلیت پیدا نشد",
        429: "محدودیت نرخ درخواست",
    }
    title = titles.get(error.status_code or 0, "خطا در ارتباط با BeatAPI")
    if (error.status_code or 0) >= 500:
        title = "خطای موقت در سرور BeatAPI"

    lines = [f"❌ <b>{html_escape(title)}</b>", "", html_escape(truncate(error.message, 450))]
    if error.code:
        lines.append(f"کد: <code>{html_escape(error.code)}</code>")
    if error.suggestions:
        lines += ["", "پیشنهادها:"]
        lines += [f"• <code>{html_escape(item)}</code>" for item in error.suggestions[:5]]
    if error.retry_after:
        lines += ["", f"⏳ لطفاً {html_escape(format_duration(error.retry_after))} دیگر تلاش کنید."]
    elif error.retryable:
        lines += ["", "🔁 این خطا موقتی است؛ کمی بعد دوباره تلاش کنید."]
    if error.request_id:
        lines += ["", f"<i>request_id: {html_escape(error.request_id)}</i>"]
    return "\n".join(lines)


def render_task_error(error: TaskFailedError) -> str:
    """Render a failed asynchronous task."""
    lines = ["❌ <b>اجرای تسک ناموفق بود</b>", ""]
    lines.append(html_escape(truncate(error.message, 450)))
    if error.code:
        lines.append(f"کد: <code>{html_escape(error.code)}</code>")
    lines.append(f"شناسه تسک: <code>{html_escape(error.task_id)}</code>")
    if error.retryable:
        lines += ["", "🔁 این خطا موقتی است؛ می‌توانید دوباره تلاش کنید."]
    return "\n".join(lines)


def render_task_timeout(error: TaskTimeoutError) -> str:
    """Render a polling timeout, including the task id."""
    return "\n".join(
        [
            "⌛️ <b>زمان انتظار به پایان رسید</b>",
            "",
            html_escape(error.message),
            f"شناسه تسک: <code>{html_escape(error.task_id)}</code>",
            "",
            "تسک ممکن است هنوز در BeatAPI در حال اجرا باشد. نتیجه نهایی را می‌توانید بعداً از داشبورد BeatAPI ببینید.",
        ]
    )


def render_items(items: list[Any], total: Any, truncated_flag: bool) -> str:
    """Render the ``items`` preview of a data result (max 12 rows)."""
    header_total = total if isinstance(total, int) else len(items)
    lines = [f"📄 <b>نتایج</b> ({header_total} مورد)"]
    for index, item in enumerate(items[:MAX_ITEM_LINES], start=1):
        if isinstance(item, Mapping):
            title = ""
            for key in ("title", "name", "text", "caption", "keyword", "word", "description"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    title = value.strip()
                    break
            url = ""
            for key in ("url", "link", "note_url", "web_url", "share_url", "permalink"):
                value = item.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    url = value
                    break
            subtitle = ""
            for key in ("desc", "description", "snippet", "summary", "author", "nickname", "user"):
                value = item.get(key)
                if isinstance(value, str) and value.strip() and value.strip() != title:
                    subtitle = value.strip()
                    break
            block = [f"{index}. <b>{html_escape(truncate(title or '(بدون عنوان)', 120))}</b>"]
            if subtitle:
                block.append(f"   {html_escape(truncate(subtitle, 160))}")
            if url:
                block.append(f"   {html_escape(url)}")
            lines.append("\n".join(block))
        else:
            lines.append(f"{index}. {html_escape(truncate(item, 160))}")
        if len("\n".join(lines)) > 3200:
            break
    if truncated_flag:
        lines += ["", "ℹ️ نتیجه بزرگ‌تر از این فهرست است."]
    return "\n".join(lines)


def render_links(links: list[str]) -> str:
    """Render a compact list of result links."""
    lines = ["🔗 <b>لینک‌های نتیجه:</b>"]
    lines += [f"• {html_escape(link)}" for link in links[:8]]
    return "\n".join(lines)


def render_card_list(cards: list[CapabilityCard], limit: int = 5) -> str:
    """Render the first few search references as a fallback list."""
    lines = ["🔎 <b>نزدیک‌ترین قابلیت‌ها:</b>"]
    for card in cards[:limit]:
        lines.append(f"• <code>{html_escape(card.reference)}</code> — {html_escape(truncate(card.label, 90))}")
    return "\n".join(lines)


def filename_from_disposition(header: str | None) -> str:
    """Extract a filename from a Content-Disposition header."""
    if not header:
        return ""
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "filename*":
            raw = value.strip().strip('"')
            if "''" in raw:
                raw = raw.split("''", 1)[1]
            return raw
        if key.lower() == "filename":
            return value.strip().strip('"')
    return ""
