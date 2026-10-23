"""Telegram command handlers and result delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Final

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.beatapi import (
    BeatAPIClient,
    BeatAPIError,
    CapabilityCard,
    CapabilityContract,
    RunResult,
    TaskFailedError,
    TaskState,
    TaskTimeoutError,
)
from app.config import Settings
from app.utils import (
    MediaFile,
    UserRateLimiter,
    build_example_input,
    chunk_text,
    extension_for,
    extract_links,
    extract_media,
    extract_output_text,
    format_duration,
    format_json,
    html_escape,
    parse_json_argument,
    render_api_error,
    render_card_list,
    render_contract,
    render_items,
    render_links,
    render_search_empty,
    render_task_error,
    render_task_timeout,
    safe_filename,
    truncate,
    validate_input,
)

LOGGER = logging.getLogger(__name__)

SESSION_KEY: Final = "session"
CARDS_CALLBACK_PREFIX: Final = "cap:"
SEARCH_LIMIT: Final = 5
MAX_ALTERNATIVE_BUTTONS: Final = 4
MAX_TEXT_CHUNKS: Final = 5
PHOTO_MAX_BYTES: Final = 9 * 1024 * 1024

_NO_PREVIEW: Final = LinkPreviewOptions(is_disabled=True)
_GENERIC_ERROR: Final = "❌ خطای غیرمنتظره‌ای رخ داد. جزئیات در لاگ سرور ثبت شد؛ لطفاً دوباره تلاش کنید."

BOT_COMMANDS: Final = (
    BotCommand("start", "معرفی ربات و امکانات"),
    BotCommand("search", "جست‌وجوی قابلیت در BeatAPI"),
    BotCommand("run", "اجرای قابلیت انتخاب‌شده با JSON"),
    BotCommand("cancel", "پاک کردن نشست فعلی"),
    BotCommand("help", "راهنمای استفاده"),
)


STATUS_LABELS: Final[dict[str, str]] = {
    "queued": "در صف",
    "pending": "در صف",
    "submitted": "ثبت‌شده",
    "processing": "در حال پردازش",
    "running": "در حال پردازش",
    "started": "در حال پردازش",
    "storyboard_ready": "استوری‌بورد آماده است",
    "requires_action": "نیازمند اقدام شما",
    "editing": "در حال ویرایش",
    "composing": "در حال ترکیب",
    "succeeded": "موفق",
    "failed": "ناموفق",
    "cancelled": "لغوشده",
    "canceled": "لغوشده",
    "expired": "منقضی",
    "timeout": "زمان‌تمام",
}


def human_status(status: str) -> str:
    """Persian label for a BeatAPI task status."""
    key = (status or "").strip().lower()
    return STATUS_LABELS.get(key, key or "نامشخص")


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class StoredContract:
    """The parts of an inspected contract that are cached per chat."""

    reference: str
    title: str
    description: str = ""
    readiness: str = ""
    execution_mode: str = ""
    price_label: str = "نامشخص"
    price_tiers: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    @classmethod
    def from_contract(cls, contract: CapabilityContract) -> StoredContract:
        return cls(
            reference=contract.reference,
            title=contract.title,
            description=contract.description,
            readiness=contract.readiness,
            execution_mode=contract.execution_mode,
            price_label=contract.price_label,
            price_tiers=tuple(contract.price_tiers()),
            input_schema=dict(contract.input_schema),
            note=contract.note,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StoredContract:
        schema = data.get("input_schema")
        tiers = data.get("price_tiers")
        return cls(
            reference=str(data.get("reference") or ""),
            title=str(data.get("title") or data.get("reference") or ""),
            description=str(data.get("description") or ""),
            readiness=str(data.get("readiness") or ""),
            execution_mode=str(data.get("execution_mode") or ""),
            price_label=str(data.get("price_label") or "نامشخص"),
            price_tiers=tuple(str(item) for item in tiers) if isinstance(tiers, list) else (),
            input_schema=schema if isinstance(schema, Mapping) else {},
            note=str(data.get("note") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "title": self.title,
            "description": self.description,
            "readiness": self.readiness,
            "execution_mode": self.execution_mode,
            "price_label": self.price_label,
            "price_tiers": list(self.price_tiers),
            "input_schema": dict(self.input_schema),
            "note": self.note,
        }

    @property
    def runnable(self) -> bool:
        return self.readiness.lower() != "listed"

    @property
    def is_async(self) -> bool:
        return self.execution_mode.lower() == "async"


def _load_session(context: ContextTypes.DEFAULT_TYPE) -> tuple[StoredContract, list[CapabilityCard]] | None:
    user_data = context.user_data
    if not user_data:
        return None
    session = user_data.get(SESSION_KEY)
    if not isinstance(session, Mapping):
        return None

    settings = _settings(context)
    created_at = session.get("created_at")
    if isinstance(created_at, (int, float)) and time.time() - created_at > settings.session_ttl_seconds:
        user_data.pop(SESSION_KEY, None)
        return None

    contract_raw = session.get("contract")
    if not isinstance(contract_raw, Mapping):
        return None
    contract = StoredContract.from_dict(contract_raw)
    if not contract.reference:
        return None

    cards_raw = session.get("cards")
    cards = (
        [CapabilityCard.from_dict(card) for card in cards_raw if isinstance(card, Mapping)]
        if isinstance(cards_raw, list)
        else []
    )
    return contract, cards


def _save_session(
    context: ContextTypes.DEFAULT_TYPE,
    contract: CapabilityContract,
    cards: list[CapabilityCard],
    query: str,
) -> None:
    if context.user_data is None:  # pragma: no cover - PTB always provides it
        return
    context.user_data[SESSION_KEY] = {
        "contract": StoredContract.from_contract(contract).to_dict(),
        "cards": [card.to_dict() for card in cards[:SEARCH_LIMIT]],
        "query": query,
        "created_at": time.time(),
    }


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    settings = context.application.bot_data.get("settings")
    if not isinstance(settings, Settings):  # pragma: no cover - wiring error
        raise TypeError("Settings are not available in bot_data")
    return settings


def _client(context: ContextTypes.DEFAULT_TYPE) -> BeatAPIClient:
    client = context.application.bot_data.get("beatapi")
    if not isinstance(client, BeatAPIClient):  # pragma: no cover - wiring error
        raise TypeError("BeatAPI client is not available in bot_data")
    return client


def _render_cards_keyboard(cards: list[CapabilityCard], selected: str) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for index, card in enumerate(cards[:SEARCH_LIMIT]):
        if card.reference == selected:
            continue
        label = truncate(card.label, 55) or card.reference
        rows.append([InlineKeyboardButton(f"🔁 {label}", callback_data=f"{CARDS_CALLBACK_PREFIX}{index}")])
        if len(rows) >= MAX_ALTERNATIVE_BUTTONS:
            break
    return InlineKeyboardMarkup(rows) if rows else None


# --------------------------------------------------------------------------- #
# Telegram I/O helpers
# --------------------------------------------------------------------------- #
async def _reply(
    message: Message | None,
    text: str,
    *,
    keyboard: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = ParseMode.HTML,
) -> Message | None:
    if message is None:
        return None
    try:
        return await message.reply_text(
            text,
            parse_mode=parse_mode,
            reply_markup=keyboard,
            link_preview_options=_NO_PREVIEW,
        )
    except TelegramError:
        LOGGER.warning("Failed to send Telegram message", exc_info=True)
        return None


async def _safe_edit(
    message: Message,
    text: str,
    *,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=_NO_PREVIEW,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            LOGGER.info("Could not edit status message: %s", exc)
    except TelegramError:
        LOGGER.warning("Could not edit status message", exc_info=True)


async def _send_text(bot: Any, chat_id: int, text: str, limit: int) -> None:
    chunks = chunk_text(text, limit)
    for chunk in chunks[:MAX_TEXT_CHUNKS]:
        await bot.send_message(chat_id=chat_id, text=chunk, link_preview_options=_NO_PREVIEW)
    if len(chunks) > MAX_TEXT_CHUNKS:
        remainder = "\n\n".join(chunks[MAX_TEXT_CHUNKS:])
        await bot.send_document(
            chat_id=chat_id,
            document=BytesIO(remainder.encode("utf-8")),
            filename="result.txt",
            caption="ادامه نتیجه",
        )


async def _send_media(
    bot: Any,
    chat_id: int,
    item: MediaFile,
    content: bytes,
    *,
    caption: str,
    filename: str,
) -> None:
    kind = item.kind if item.kind in {"image", "video", "audio", "file"} else "file"
    safe_name = safe_filename(filename or item.filename or "result")
    if "." not in safe_name:
        safe_name += extension_for(item.mime_type, kind)
    cap = truncate(caption, 900)

    def stream() -> BytesIO:
        buffer = BytesIO(content)
        buffer.name = safe_name
        return buffer

    try:
        if kind == "image" and len(content) <= PHOTO_MAX_BYTES:
            await bot.send_photo(chat_id=chat_id, photo=stream(), caption=cap)
        elif kind == "video":
            await bot.send_video(
                chat_id=chat_id,
                video=stream(),
                caption=cap,
                filename=safe_name,
                supports_streaming=True,
            )
        elif kind == "audio":
            await bot.send_audio(chat_id=chat_id, audio=stream(), caption=cap, filename=safe_name)
        else:
            await bot.send_document(chat_id=chat_id, document=stream(), caption=cap, filename=safe_name)
    except BadRequest:
        LOGGER.info("Telegram rejected %s as %s; retrying as document", safe_name, kind)
        await bot.send_document(
            chat_id=chat_id,
            document=stream(),
            caption=cap,
            filename=safe_name,
        )


async def deliver_payload(
    bot: Any,
    client: BeatAPIClient,
    chat_id: int,
    payload: Mapping[str, Any],
    *,
    title: str,
    settings: Settings,
) -> int:
    """Deliver a result to Telegram: files first, then text, items, links.

    Returns the number of Telegram messages sent. A JSON fallback guarantees the
    user always receives something.
    """
    delivered = 0
    used_urls: set[str] = set()

    media = extract_media(payload, limit=settings.max_attachments)
    for item in media:
        try:
            downloaded = await client.download(item.url, max_bytes=settings.max_download_bytes)
        except BeatAPIError as exc:
            LOGGER.warning("Skipping media download (%s): %s", exc.code, item.url)
            continue
        try:
            await _send_media(
                bot,
                chat_id,
                item,
                downloaded.content,
                caption=title,
                filename=downloaded.filename or item.filename,
            )
            used_urls.add(item.url)
            delivered += 1
        except TelegramError:
            LOGGER.warning("Telegram rejected media %s", item.url, exc_info=True)

    text = extract_output_text(payload)
    if text:
        await _send_text(bot, chat_id, text, settings.max_result_chars)
        delivered += 1

    items = payload.get("items")
    if isinstance(items, list) and items:
        rendered = render_items(items, payload.get("items_total"), bool(payload.get("truncated")))
        await bot.send_message(
            chat_id=chat_id,
            text=rendered,
            parse_mode=ParseMode.HTML,
            link_preview_options=_NO_PREVIEW,
        )
        delivered += 1

    links = [link for link in extract_links(payload, limit=8, exclude=used_urls) if link not in used_urls]
    if links:
        await bot.send_message(
            chat_id=chat_id,
            text=render_links(links),
            parse_mode=ParseMode.HTML,
            link_preview_options=_NO_PREVIEW,
        )
        delivered += 1

    if delivered == 0:
        pretty = format_json(payload, max_chars=settings.max_result_chars)
        if len(pretty) <= settings.max_result_chars - 20:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"<pre>{html_escape(pretty)}</pre>",
                    parse_mode=ParseMode.HTML,
                    link_preview_options=_NO_PREVIEW,
                )
            except BadRequest:
                await _send_text(bot, chat_id, pretty, settings.max_result_chars)
        else:
            await bot.send_document(
                chat_id=chat_id,
                document=BytesIO(json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")),
                filename="result.json",
                caption=truncate(title, 900),
            )
        delivered += 1

    return delivered


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — welcome message."""
    text = (
        "👋 <b>سلام! من ربات BeatAPI هستم.</b>\n"
        "با یک کلید، مدل‌ها، داده‌های شبکه‌های اجتماعی، جست‌وجوی وب و "
        "گردش‌کارهای تصویر/ویدیو را از BeatAPI اجرا می‌کنم.\n\n"
        "<b>چطور کار می‌کنم؟</b>\n"
        "1️⃣ با /search یک قابلیت پیدا می‌کنم و مشخصات ورودی و قیمتش را نشان می‌دهم.\n"
        "2️⃣ با /run و یک JSON، همان قابلیت را اجرا می‌کنم.\n"
        "3️⃣ نتیجه — متن، لینک یا فایل — مستقیم برای شما ارسال می‌شود.\n\n"
        "<b>دستورها</b>\n"
        "🔎 <code>/search &lt;عبارت&gt;</code> — جست‌وجو، مثلاً <code>/search tiktok user profile</code>\n"
        '▶️ <code>/run &lt;json&gt;</code> — اجرا، مثلاً <code>/run {"username": "nasa"}</code>\n'
        "🧹 <code>/cancel</code> — پاک کردن نشست\n"
        "ℹ️ <code>/help</code> — راهنمای کامل\n\n"
        "برای شروع یک جست‌وجو بنویسید. 🚀"
    )
    await _reply(update.effective_message, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — usage guide."""
    text = (
        "<b>راهنمای استفاده</b>\n\n"
        "<b>1) جست‌وجو</b>\n"
        "<code>/search tiktok user profile</code>\n"
        "<code>/search 小红书 搜索笔记</code>\n"
        "<code>/search image model</code>\n"
        "بهترین نتیجه به‌صورت خودکار Inspect می‌شود و نام قابلیت، ورودی‌های الزامی و قیمت "
        "نمایش داده می‌شود. با دکمه‌های زیر پیام هم می‌توانید گزینه‌های دیگر را ببینید.\n\n"
        "<b>2) اجرا</b>\n"
        '<code>/run {"keyword": "AI 视频"}</code>\n'
        "ورودی باید با شمای نمایش‌داده‌شده سازگار باشد؛ فیلد ناشناخته یا فیلد الزامیِ جاافتاده "
        "قبل از ارسال به BeatAPI گزارش می‌شود.\n\n"
        "<b>3) نتیجه</b>\n"
        "نتایج همگام (متن، داده، جست‌وجوی وب) بلافاصله ارسال می‌شوند. برای تسک‌های ناهمگام "
        "(تصویر، ویدیو، workflow) پیام «⏳ در حال پردازش…» به‌روزرسانی می‌شود و فایل نهایی "
        "به‌محض آماده‌شدن ارسال می‌گردد.\n\n"
        "<b>نکته‌ها</b>\n"
        "• هر اجرا از اعتبار حساب BeatAPI شما کم می‌کند؛ قیمت قبل از اجرا نشان داده می‌شود.\n"
        "• <code>/cancel</code> نشست فعلی را پاک می‌کند و برای اجرای قابلیت دیگری باید دوباره "
        "جست‌وجو کنید.\n"
        "• کلید API فقط روی سرور نگهداری می‌شود و هرگز در چت نمایش داده نمی‌شود."
    )
    await _reply(update.effective_message, text)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <query> — search, inspect and cache the best capability."""
    message = update.effective_message
    query = _raw_argument(message)
    if not query:
        await _reply(
            message,
            "🔎 عبارت جست‌وجو را بعد از دستور بنویسید.\n"
            "مثال: <code>/search tiktok user profile</code>\n"
            "مثال: <code>/search image model</code>",
        )
        return
    if len(query) > 400:
        await _reply(message, "❌ عبارت جست‌وجو طولانی است (حداکثر ۴۰۰ نویسه).")
        return

    client = _client(context)
    status_message = await _reply(message, "🔍 در حال جست‌وجو در BeatAPI…", parse_mode=None)
    try:
        result = await client.search(query, limit=SEARCH_LIMIT)
    except BeatAPIError as exc:
        if status_message is not None:
            await _safe_edit(status_message, render_api_error(exc))
        else:
            await _reply(message, render_api_error(exc))
        return

    if not result.cards:
        text = render_search_empty(query, result.hints, result.groups, result.terms)
        if status_message is not None:
            await _safe_edit(status_message, text)
        else:
            await _reply(message, text)
        return

    card = result.pick()
    if card is None:  # pragma: no cover - defensive
        await _reply(message, "❌ هیچ قابلیتی پیدا نشد.")
        return

    try:
        contract = await client.inspect(card.reference)
    except BeatAPIError as exc:
        card_list = render_card_list(result.cards)
        text = f"{card_list}\n\n{render_api_error(exc)}"
        if status_message is not None:
            await _safe_edit(status_message, text)
        else:
            await _reply(message, text)
        return

    _save_session(context, contract, list(result.cards), query)
    keyboard = _render_cards_keyboard(list(result.cards), contract.reference)
    text = render_contract(contract)
    if result.recommended_why:
        text += f"\n\n💡 {html_escape(truncate(result.recommended_why, 200))}"

    if status_message is not None:
        await _safe_edit(status_message, text, keyboard=keyboard)
    else:
        await _reply(message, text, keyboard=keyboard)


async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/run <json> — validate input, run the cached capability, deliver result."""
    message = update.effective_message
    session = _load_session(context)
    if session is None:
        await _reply(
            message,
            "❗️ ابتدا با <code>/search</code> یک قابلیت پیدا کنید، بعد با <code>/run</code> اجرا کنید.",
        )
        return

    contract, _cards = session
    raw = _raw_argument(message)
    if not raw:
        example = build_example_input(contract.input_schema)
        example_text = json.dumps(example, ensure_ascii=False) if example else "{}"
        await _reply(
            message,
            "▶️ ورودی JSON را بعد از دستور بنویسید.\n"
            f"مثال: <code>/run {html_escape(example_text)}</code>\n"
            "برای دیدن شمای کامل، دوباره /search بزنید.",
        )
        return

    try:
        payload = parse_json_argument(raw)
    except ValueError as exc:
        await _reply(
            message,
            f'❌ {html_escape(str(exc))}\nمثال درست: <code>/run {{"prompt": "a cat"}}</code>',
        )
        return

    inputs = _extract_inputs(payload, contract.input_schema)
    if inputs is None:
        await _reply(message, "❌ مقدار <code>input</code> باید یک شیء JSON باشد.")
        return

    errors = validate_input(contract.input_schema, inputs)
    if errors:
        lines = ["❌ <b>ورودی با شمای این قابلیت سازگار نیست:</b>"]
        lines += [f"• {html_escape(error)}" for error in errors[:8]]
        example = build_example_input(contract.input_schema)
        if example:
            lines += [
                "",
                "نمونه درست:",
                f"<code>/run {html_escape(json.dumps(example, ensure_ascii=False))}</code>",
            ]
        await _reply(message, "\n".join(lines))
        return

    if not contract.runnable:
        await _reply(
            message,
            "⛔️ این قابلیت از طریق Run قابل اجرا نیست (readiness=listed).\n"
            "با <code>/search</code> گزینه دیگری پیدا کنید.",
        )
        return

    client = _client(context)
    settings = _settings(context)
    processing = await _reply(
        message,
        f"⏳ در حال پردازش…\n💵 قیمت اعلام‌شده: {html_escape(contract.price_label)}",
    )

    view = "preview" if contract.reference.startswith("data:") else None
    try:
        result = await client.run(
            contract.reference,
            inputs,
            idempotency_key=secrets.token_hex(16),
            view=view,
            max_items=10 if view else None,
        )
    except BeatAPIError as exc:
        await _handle_processing_error(processing, message, render_api_error(exc))
        return
    except Exception:  # never leak a traceback to Telegram
        LOGGER.exception("Unexpected error while starting BeatAPI run")
        await _handle_processing_error(processing, message, _GENERIC_ERROR)
        return

    if result.is_async:
        await _follow_task(context, client, processing, message, contract, result, settings)
    else:
        await _deliver_sync(context, client, processing, message, contract, result, settings)


def _extract_inputs(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> dict[str, Any] | None:
    """Accept both ``{"prompt": ...}`` and the full ``{"input": {...}}`` form."""
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    nested = payload.get("input")
    wrapper_keys = {"input", "reference", "idempotency_key"}

    if isinstance(nested, Mapping) and set(payload) <= wrapper_keys and "input" not in properties:
        return dict(nested)
    if "input" in payload and isinstance(nested, Mapping) and "input" not in properties:
        return dict(nested)
    if "input" in payload and not isinstance(nested, Mapping) and "input" not in properties:
        return None

    filtered = {key: value for key, value in payload.items() if key not in {"reference", "idempotency_key"}}
    return dict(filtered)


async def _handle_processing_error(processing: Message | None, message: Message | None, text: str) -> None:
    if processing is not None:
        await _safe_edit(processing, text)
    else:
        await _reply(message, text)


async def _deliver_sync(
    context: ContextTypes.DEFAULT_TYPE,
    client: BeatAPIClient,
    processing: Message | None,
    message: Message | None,
    contract: StoredContract,
    result: RunResult,
    settings: Settings,
) -> None:
    if processing is not None:
        await _safe_edit(processing, f"✅ <b>{html_escape(contract.title)}</b>\nنتیجه آماده شد؛ در حال ارسال…")
    chat_id = message.chat_id if message is not None else None
    if chat_id is None:  # pragma: no cover - Telegram always provides a chat
        return
    try:
        await deliver_payload(
            context.bot,
            client,
            chat_id,
            result.payload,
            title=contract.title,
            settings=settings,
        )
    except TelegramError:
        LOGGER.warning("Failed to deliver a sync result", exc_info=True)
        await _reply(message, "⚠️ نتیجه دریافت شد اما ارسال آن در تلگرام ناموفق بود. لطفاً دوباره تلاش کنید.")
    except Exception:  # delivery must never crash the handler
        LOGGER.exception("Unexpected error while delivering a sync result")
        await _reply(message, _GENERIC_ERROR)


async def _follow_task(
    context: ContextTypes.DEFAULT_TYPE,
    client: BeatAPIClient,
    processing: Message | None,
    message: Message | None,
    contract: StoredContract,
    result: RunResult,
    settings: Settings,
) -> None:
    task_id = result.task_id or ""
    chat_id = message.chat_id if message is not None else None
    if processing is None or chat_id is None:  # pragma: no cover - Telegram always provides both
        await _reply(message, f"⏳ تسک ثبت شد: <code>{html_escape(task_id)}</code>")
        return

    started = time.monotonic()
    state = {"last_edit": started}
    await _safe_edit(
        processing,
        f"⏳ <b>{html_escape(contract.title)}</b>\n"
        f"وضعیت: {html_escape(human_status(result.status or 'queued'))}\n"
        f"شناسه تسک: <code>{html_escape(task_id)}</code>\n"
        "نتیجه به‌محض آماده‌شدن ارسال می‌شود.",
    )

    async def progress(current: TaskState) -> None:
        now = time.monotonic()
        if now - state["last_edit"] < settings.progress_update_seconds:
            return
        state["last_edit"] = now
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        except TelegramError:
            LOGGER.debug("send_chat_action failed", exc_info=True)
        await _safe_edit(
            processing,
            f"⏳ <b>{html_escape(contract.title)}</b>\n"
            f"وضعیت: {html_escape(human_status(current.status))}\n"
            f"زمان سپری‌شده: {html_escape(format_duration(now - started))}\n"
            f"<code>{html_escape(task_id)}</code>",
        )

    try:
        final_state = await client.wait_for_task(
            task_id,
            reference=contract.reference,
            timeout_seconds=settings.poll_timeout_seconds,
            on_progress=progress,
        )
    except TaskTimeoutError as exc:
        await _safe_edit(processing, render_task_timeout(exc))
        return
    except TaskFailedError as exc:
        await _safe_edit(processing, render_task_error(exc))
        return
    except BeatAPIError as exc:
        await _safe_edit(processing, render_api_error(exc))
        return

    elapsed = time.monotonic() - started
    await _safe_edit(
        processing,
        f"✅ <b>{html_escape(contract.title)}</b>\n"
        f"تکمیل شد در {html_escape(format_duration(elapsed))}؛ در حال ارسال نتیجه…",
    )
    try:
        await deliver_payload(
            context.bot,
            client,
            chat_id,
            final_state.payload,
            title=contract.title,
            settings=settings,
        )
    except TelegramError:
        LOGGER.warning("Failed to deliver an async result", exc_info=True)
        await _reply(message, "⚠️ نتیجه آماده شد اما ارسال فایل در تلگرام ناموفق بود.")
    except Exception:  # delivery must never crash the handler
        LOGGER.exception("Unexpected error while delivering an async result")
        await _reply(message, _GENERIC_ERROR)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — clear the cached capability."""
    if context.user_data is not None:
        context.user_data.pop(SESSION_KEY, None)
    await _reply(
        update.effective_message,
        "🧹 نشست پاک شد. برای شروع دوباره از <code>/search</code> استفاده کنید.",
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update.effective_message,
        "🤔 این دستور را نمی‌شناسم. دستورهای موجود: /start، /search، /run، /cancel، /help",
    )


async def on_capability_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline keyboard: inspect another result from the last search."""
    callback = update.callback_query
    if callback is None or not callback.data:
        return

    try:
        index = int(callback.data.removeprefix(CARDS_CALLBACK_PREFIX))
    except ValueError:
        await _answer_callback(callback, "دکمه نامعتبر است.")
        return

    session = _load_session(context)
    if session is None:
        await _answer_callback(callback, "نشست منقضی شده است؛ دوباره /search بزنید.", alert=True)
        return

    _contract, cards = session
    if not 0 <= index < len(cards):
        await _answer_callback(callback, "این گزینه در دسترس نیست؛ دوباره /search بزنید.", alert=True)
        return

    await _answer_callback(callback)
    card = cards[index]
    client = _client(context)
    try:
        contract = await client.inspect(card.reference)
    except BeatAPIError as exc:
        if callback.message is not None:
            await _safe_edit(callback.message, render_api_error(exc))
        return

    if context.user_data is not None:
        session_data = context.user_data.get(SESSION_KEY)
        original_query = ""
        if isinstance(session_data, Mapping):
            original_query = str(session_data.get("query") or "")
        _save_session(context, contract, cards, original_query)

    keyboard = _render_cards_keyboard(cards, contract.reference)
    if callback.message is not None:
        await _safe_edit(callback.message, render_contract(contract), keyboard=keyboard)


async def _answer_callback(query: Any, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await query.answer(text=truncate(text, 190) if text else None, show_alert=alert)
    except TelegramError:
        LOGGER.debug("Could not answer callback query", exc_info=True)


# --------------------------------------------------------------------------- #
# Guards & error handling
# --------------------------------------------------------------------------- #
def _raw_argument(message: Message | None) -> str:
    if message is None:
        return ""
    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _build_guard(
    settings: Settings, limiter: UserRateLimiter
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
    async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.is_bot:
            return

        if settings.allowed_user_id_set and user.id not in settings.allowed_user_id_set:
            await _deny(update, "⛔️ این ربات خصوصی است و شما مجاز به استفاده از آن نیستید.")
            raise ApplicationHandlerStop

        message = update.effective_message
        text = (message.text if message is not None else "") or ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command in {"/start", "/help"}:
            return

        if not limiter.allow(user.id):
            wait = limiter.retry_after(user.id)
            await _deny(
                update,
                f"🐢 تعداد درخواست‌ها زیاد است. لطفاً {format_duration(wait)} دیگر تلاش کنید.",
            )
            raise ApplicationHandlerStop

    return guard


async def _deny(update: Update, text: str) -> None:
    if update.callback_query is not None:
        await _answer_callback(update.callback_query, text, alert=True)
        return
    await _reply(update.effective_message, text, parse_mode=None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the failure server-side and tell the user something safe."""
    error = context.error
    LOGGER.error("Unhandled error while processing an update: %s", type(error).__name__, exc_info=error)

    if isinstance(error, (TimedOut, NetworkError)):
        user_text = "🌐 ارتباط با تلگرام یا BeatAPI برقرار نشد. لطفاً دوباره تلاش کنید."
    else:
        user_text = _GENERIC_ERROR

    message = update.effective_message if isinstance(update, Update) else None
    if message is not None:
        await _reply(message, user_text, parse_mode=None)


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #
def _post_init(settings: Settings) -> Callable[[Application], Awaitable[None]]:
    async def post_init(application: Application) -> None:
        try:
            await application.bot.set_my_commands(list(BOT_COMMANDS))
        except TelegramError:  # pragma: no cover - non fatal (network hiccup)
            LOGGER.warning("Could not register bot commands", exc_info=True)

        if settings.mode == "polling" and settings.polling_max_runtime_seconds > 0:
            limit = settings.polling_max_runtime_seconds
            LOGGER.info("Polling will stop gracefully after %d seconds", limit)

            async def stop_later() -> None:
                await asyncio.sleep(limit)
                LOGGER.info("Polling runtime limit reached; stopping the bot")
                application.stop_running()

            application.create_task(stop_later())

    return post_init


async def _post_shutdown(application: Application) -> None:
    """Release the HTTP client when the application stops."""
    client = application.bot_data.get("beatapi")
    if isinstance(client, BeatAPIClient):
        await client.aclose()
        LOGGER.info("BeatAPI client closed")


def build_application(settings: Settings) -> Application:
    """Create the PTB application with all handlers wired up."""
    builder = ApplicationBuilder().token(settings.telegram_token)
    if settings.mode == "webhook":
        # Updates arrive through FastAPI; no polling updater is needed.
        builder = builder.updater(None)
    builder = (
        builder.concurrent_updates(settings.concurrent_updates)
        .read_timeout(30)
        .write_timeout(120)
        .connect_timeout(15)
        .pool_timeout(10)
        .post_init(_post_init(settings))
        .post_shutdown(_post_shutdown)
    )

    application = builder.build()
    limiter = UserRateLimiter(settings.rate_limit_per_minute)
    application.bot_data["settings"] = settings
    application.bot_data["rate_limiter"] = limiter
    application.bot_data["beatapi"] = BeatAPIClient.from_settings(settings)

    application.add_handler(TypeHandler(Update, _build_guard(settings, limiter)), group=-1)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(on_capability_selected, pattern=f"^{CARDS_CALLBACK_PREFIX}"))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)
    return application
