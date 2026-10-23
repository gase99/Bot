"""FastAPI application used when ``MODE=webhook``.

Telegram posts updates to ``WEBHOOK_URL``; the endpoint authenticates them with
the secret token that Telegram echoes back, then hands them to the PTB update
queue. The HTTP response is returned immediately so Telegram never times out
while a long BeatAPI task is being processed.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from app.config import Settings
from app.handlers import build_application

LOGGER = logging.getLogger(__name__)

MAX_UPDATE_BYTES: Final = 1024 * 1024  # Telegram updates are far smaller than 1 MiB


def create_app(settings: Settings) -> FastAPI:
    """Build the FastAPI app with its Telegram lifecycle wired into lifespan."""

    webhook_path = settings.webhook_path
    secret = settings.webhook_secret

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        application = build_application(settings)
        try:
            await application.initialize()
            await application.bot.set_webhook(
                url=settings.webhook_url,
                secret_token=secret,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                max_connections=settings.webhook_max_connections,
            )
            await application.start()
            app.state.telegram_app = application
            LOGGER.info("Webhook registered at %s", settings.webhook_url)
            yield
        finally:
            app.state.telegram_app = None
            try:
                await application.bot.delete_webhook(drop_pending_updates=False)
            except TelegramError:
                LOGGER.warning("Could not delete the Telegram webhook", exc_info=True)
            try:
                if application.running:
                    await application.stop()
                await application.shutdown()
                LOGGER.info("Telegram application stopped")
            except Exception:  # cleanup must never mask the original error
                LOGGER.warning("Error while shutting down the Telegram application", exc_info=True)

    app = FastAPI(
        title="telegram-beatapi-bot",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/", include_in_schema=False)
    async def index() -> dict[str, str]:
        return {"service": "telegram-beatapi-bot", "mode": settings.mode, "webhook_path": webhook_path}

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        ready = isinstance(getattr(app.state, "telegram_app", None), Application)
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ok" if ready else "starting", "mode": settings.mode},
        )

    @app.post(webhook_path)
    async def telegram_webhook(request: Request) -> Response:
        token = request.headers.get("x-telegram-bot-api-secret-token", "")
        if secret and not hmac.compare_digest(token, secret):
            LOGGER.warning("Rejected a webhook call with an invalid secret token")
            return Response(status_code=status.HTTP_401_UNAUTHORIZED)

        body = await request.body()
        if len(body) > MAX_UPDATE_BYTES:
            LOGGER.warning("Rejected an oversized update (%d bytes)", len(body))
            return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        application = getattr(request.app.state, "telegram_app", None)
        if not isinstance(application, Application):
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

        try:
            data: Any = json.loads(body)
        except ValueError:
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        try:
            update = Update.de_json(data, application.bot)
        except (TypeError, ValueError):
            LOGGER.warning("Could not deserialize a Telegram update")
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        await application.update_queue.put(update)
        return Response(status_code=status.HTTP_200_OK)

    return app
