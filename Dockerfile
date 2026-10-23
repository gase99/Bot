# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Production image for the Telegram ↔ BeatAPI bot.
# Build:  docker build -t telegram-beatapi-bot .
# Run:    docker run --env-file .env -p 8443:8443 telegram-beatapi-bot
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Unprivileged user (no login shell, own group).
RUN addgroup --system bot \
    && adduser --system --ingroup bot --shell /usr/sbin/nologin bot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bot:bot app ./app

USER bot

EXPOSE 8443

# In webhook mode, probe /healthz; in polling mode there is no HTTP server, so
# the check succeeds immediately.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; mode=os.getenv('MODE','webhook').lower(); sys.exit(0) if mode!='webhook' else sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('PORT','8443'), timeout=4).status==200 else 1)"

CMD ["python", "-m", "app.main"]
