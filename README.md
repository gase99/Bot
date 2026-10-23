# Telegram Bot for BeatAPI

[![CI](https://github.com/gase99/Bot/actions/workflows/ci.yml/badge.svg)](https://github.com/gase99/Bot/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

ربات تلگرامی آماده‌ی تولید که به [BeatAPI](https://beatapi.io) وصل می‌شود و چرخه‌ی
**Search → Inspect → Run** را در سه دستور ساده در اختیار کاربر می‌گذارد: جست‌وجوی قابلیت،
دیدن ورودی‌ها و قیمت، و اجرای آن با یک JSON — و در پایان، **نتیجه‌ی واقعی** (متن، لینک یا فایل)
را در تلگرام تحویل می‌دهد.

این ربات با سه روش اجرا می‌شود:

| روش | مناسب برای | حالت |
| --- | --- | --- |
| **A. میزبانی ابری** (Railway / Render / Fly.io) | استفاده‌ی ۲۴ ساعته و پایدار | `MODE=webhook` |
| **B. GitHub Actions** | دمو و اجرای بدون سرور (با محدودیت ۶ ساعته) | `MODE=polling` |
| **C. لوکال / Docker** | توسعه و تست | هر دو حالت |

---

## فهرست

- [قابلیت‌ها](#قابلیتها)
- [معماری](#معماری)
- [ساختار پروژه](#ساختار-پروژه)
- [شروع سریع](#شروع-سریع)
- [دستورهای ربات](#دستورهای-ربات)
- [متغیرهای محیطی](#متغیرهای-محیطی)
- [نحوه‌ی اتصال به BeatAPI](#نحوهی-اتصال-به-beatapi)
- [استقرار A: میزبانی ابری (Webhook)](#استقرار-a-میزبانی-ابری-webhook)
- [استقرار B: GitHub Actions (Polling)](#استقرار-b-github-actions-polling)
- [استقرار C: لوکال و Docker](#استقرار-c-لوکال-و-docker)
- [تست و کیفیت کد](#تست-و-کیفیت-کد)
- [نکات امنیتی](#نکات-امنیتی)
- [عیب‌یابی](#عیبیابی)
- [English summary](#english-summary)
- [مجوز](#مجوز)

---

## قابلیت‌ها

- 🔎 **`/search`** — جست‌وجوی کاتالوگ BeatAPI (بدون نیاز به کلید) و Inspect خودکار بهترین نتیجه:
  نام قابلیت، ورودی‌های الزامی/اختیاری، قیمت و حالت اجرا (sync/async).
- ▶️ **`/run`** — اعتبارسنجی JSON کاربر با `input_schema` پیش از ارسال، اجرای قابلیت و تحویل نتیجه.
- ⏳ **مدیریت تسک‌های ناهمگام** — پیام «در حال پردازش…» با به‌روزرسانی وضعیت و زمان، سپس ارسال
  فایل تصویر/ویدیو/سند یا متن نهایی. هرگز فقط `task_id` تحویل داده نمی‌شود.
- 🧹 **`/cancel`** — پاک کردن نشست (قابلیت انتخاب‌شده) و شروع دوباره.
- 🛡 **امنیت** — کلید BeatAPI فقط سمت سرور است، در لاگ‌ها ماسک می‌شود، هیچ‌گاه به تلگرام نمی‌رود و
  روی دانلود فایل‌ها هم فرستاده نمی‌شود. خطاها بدون stack trace به کاربر نشان داده می‌شوند.
- ♻️ **سیاست تلاش مجدد** — `error.retryable=false` هرگز تکرار نمی‌شود؛ HTTP 429 با احترام به
  `Retry-After` (تا سقف مشخص) دوباره امتحان می‌شود؛ تلاش‌ها با backoff نمایی و jitter.
- 🧪 **تست‌های واحد** بدون شبکه، با `httpx.MockTransport` — ۴۳ تست.
- 🚀 **سه مسیر استقرار** با Dockerfile، docker-compose و دو ورک‌فلوی GitHub Actions.
- 🐳 **تصویر Docker سبک** با کاربر غیر root و healthcheck.

---

## معماری

```text
Telegram ──update──▶  PTB Application  ──▶  app/handlers.py
                              │                   │
                              │                   ├─ /search ─▶ BeatAPIClient.search()  (keyless)
                              │                   │            BeatAPIClient.inspect() (keyless)
                              │                   ├─ /run    ─▶ validate input
                              │                   │            BeatAPIClient.run()     (Bearer key)
                              │                   │            BeatAPIClient.wait_for_task()
                              │                   │                 └─ GET /v1/tasks/{id}
                              │                   └─ download media (no Authorization header)
                              │
          MODE=webhook ─▶ FastAPI (app/webhook.py) ─▶ uvicorn :PORT  /webhook, /healthz
          MODE=polling ─▶ getUpdates long polling (run_polling)
```

- **Webhook:** تلگرام هر آپدیت را به `WEBHOOK_URL` می‌فرستد؛ نقطه‌ی پایانی با هدر
  `X-Telegram-Bot-Api-Secret-Token` احراز هویت می‌شود و پاسخ بلافاصله `200` است تا هنگام
  پردازش تسک‌های طولانی، تلگرام timeout ندهد.
- **Polling:** بدون نیاز به دامنه/HTTPS؛ مناسب GitHub Actions یا اجرای پشت NAT.
- **هم‌زمانی:** تا ۳۲ آپدیت به‌صورت هم‌زمان پردازش می‌شود تا یک تسک ویدیویی طولانی، بقیه‌ی
  کاربران را بلاک نکند (`CONCURRENT_UPDATES`).

---

## ساختار پروژه

```text
.
├─ app/
│  ├─ __init__.py          # نسخه‌ی پکیج
│  ├─ main.py              # ورودی، پیکربندی لاگ و انتخاب webhook/polling
│  ├─ config.py            # Settings با pydantic-settings و اعتبارسنجی
│  ├─ beatapi.py           # کلاینت async: search / inspect / run / wait_for_task / download
│  ├─ handlers.py          # دستورهای تلگرام، اعتبارسنجی ورودی و تحویل نتیجه
│  ├─ webhook.py           # اپ FastAPI برای MODE=webhook
│  └─ utils.py             # رندر نتیجه، اعتبارسنجی JSON Schema، فایل/لینک، محدودیت نرخ
├─ tests/
│  └─ test_beatapi.py      # تست‌های واحد با httpx.MockTransport
├─ .github/workflows/
│  ├─ ci.yml               # ruff + pytest + docker build در push/PR
│  └─ run-bot.yml          # اجرای polling با workflow_dispatch و schedule
├─ .env.example
├─ .gitignore
├─ Dockerfile
├─ docker-compose.yml
├─ requirements.txt
├─ requirements-dev.txt
├─ README.md
├─ LICENSE
└─ Makefile
```

---

## شروع سریع

### ۱) پیش‌نیازها

- Python **3.11+** (یا Docker)
- توکن ربات از [@BotFather](https://t.me/BotFather)
- کلید BeatAPI از [داشبورد](https://beatapi.io/dashboard/apikeys)

### ۲) کلون و نصب

```bash
git clone https://github.com/gase99/Bot.git
cd Bot

cp .env.example .env
# .env را باز کنید و TELEGRAM_BOT_TOKEN و BEATAPI_API_KEY را پر کنید

python3 -m venv .venv
source .venv/bin/activate        # ویندوز: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

یا با Make:

```bash
make install-dev
```

### ۳) انتخاب حالت و اجرا

**لوکال (پیشنهادی برای تست):** `MODE=polling` را در `.env` بگذارید — به دامنه و HTTPS نیازی نیست:

```bash
make run-polling
# یا: MODE=polling python -m app.main
```

**وب‌هوک:** به یک دامنه‌ی عمومی HTTPS نیاز دارید (Railway/Render/Fly یا ngrok/cloudflared).

```bash
# .env
MODE=webhook
WEBHOOK_URL=https://your.domain/webhook
PORT=8443

python -m app.main
```

سپس در تلگرام `/start` بزنید. اگر ربات جواب نداد، بخش [عیب‌یابی](#عیبیابی) را ببینید.

> **نکته:** مقدار `WEBHOOK_URL` باید دقیقاً همان مسیری باشد که تلگرام باید صدا بزند؛ پیش‌فرض
> مسیر `/webhook` است. اگر فقط `https://your.domain` بنویسید، ربات خودش `/webhook` را اضافه می‌کند.

---

## دستورهای ربات

| دستور | کار |
| --- | --- |
| `/start` | خوش‌آمدگویی و فهرست کوتاه امکانات |
| `/search <query>` | جست‌وجو + Inspect، ذخیره‌ی reference و schema در نشست، نمایش نام، ورودی‌های الزامی و قیمت |
| `/run <json>` | اعتبارسنجی JSON با schema، اجرای قابلیت و ارسال نتیجه (متن/لینک/فایل) |
| `/cancel` | پاک کردن نشست |
| `/help` | راهنمای کوتاه |

### نمونه‌ها

```text
/search tiktok user profile
/search 小红书 搜索笔记
/search image model
/search text model
```

پاسخ `/search` چیزی شبیه این است:

```text
🎯 TikTok user profile
data:tiktok.user.profile

Fetch a public TikTok profile.

💵 قیمت: $0.002
⚙️ اجرا: غیرهمگام (async) — وضعیت: آماده اجرا
📥 ورودی‌های الزامی:
 • username (string) — TikTok handle without the @
📎 اختیاری: fresh

▶️ اجرا:
/run {"username": "text"}
```

سپس:

```text
/run {"username": "nasa"}
```

- برای مدل‌های متنی: `/run {"input": "یک جمله درباره‌ی میلان بنویس"}`
- برای تصویر/ویدیو: فیلدهای همان schema، مثلاً `/run {"prompt": "a red fox in snow", "aspect_ratio": "16:9"}`
- اگر کل شیء را با wrapper بفرستید هم پذیرفته می‌شود: `/run {"input": {"username": "nasa"}}`

**رفتار تسک‌های ناهمگام:** پس از `/run` یک پیام `⏳ در حال پردازش…` ارسال می‌شود که هر
`PROGRESS_UPDATE_SECONDS` (پیش‌فرض ۲۰ ثانیه) با وضعیت و زمان سپری‌شده به‌روزرسانی می‌شود؛
به‌محض `succeeded`، فایل نتیجه دانلود و ارسال می‌شود. در صورت `failed`، کد خطا و
قابل‌تلاش‌بودن مجدد نمایش داده می‌شود.

---

## متغیرهای محیطی

| متغیر | الزامی | پیش‌فرض | توضیح |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | — | توکن ربات |
| `BEATAPI_API_KEY` | ✅ | — | کلید BeatAPI (فقط سمت سرور) |
| `MODE` | — | `webhook` | `webhook` یا `polling` |
| `WEBHOOK_URL` | در حالت webhook | — | آدرس عمومی HTTPS، مثل `https://bot.example.com/webhook` |
| `PORT` | — | `8443` | پورت سرور وب‌هوک |
| `LOG_LEVEL` | — | `INFO` | `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG` |
| `BEATAPI_BASE_URL` | — | `https://api.beatapi.io` | آدرس API |
| `BEATAPI_TIMEOUT_SECONDS` | — | `60` | timeout هر درخواست |
| `BEATAPI_MAX_RETRIES` | — | `2` | تعداد تلاش مجدد برای خطاهای retryable |
| `BEATAPI_RETRY_BACKOFF_SECONDS` | — | `1.5` | پایه‌ی backoff نمایی |
| `BEATAPI_MAX_RETRY_AFTER_SECONDS` | — | `30` | سقف احترام به `Retry-After` |
| `POLL_INTERVAL_MIN_SECONDS` | — | `5` | حداقل فاصله‌ی polling |
| `POLL_INTERVAL_MAX_SECONDS` | — | `10` | حداکثر فاصله‌ی polling (با jitter) |
| `POLL_TIMEOUT_SECONDS` | — | `900` | حداکثر انتظار برای یک تسک |
| `PROGRESS_UPDATE_SECONDS` | — | `20` | فاصله‌ی به‌روزرسانی پیام پیشرفت |
| `MAX_DOWNLOAD_BYTES` | — | `50331648` (48MB) | سقف حجم فایل دریافتی (سقف تلگرام ۵۰MB) |
| `MAX_RESULT_CHARS` | — | `3500` | طول هر پیام متنی |
| `MAX_ATTACHMENTS` | — | `3` | حداکثر فایل ارسالی از هر نتیجه |
| `WEBHOOK_SECRET_TOKEN` | — | مشتق از توکن | مقدار هدر امنیتی وب‌هوک |
| `WEBHOOK_MAX_CONNECTIONS` | — | `40` | حداکثر اتصال وب‌هوک تلگرام |
| `ALLOWED_USER_IDS` | — | خالی (همه) | فهرست شناسه‌های مجاز، جداشده با کاما |
| `RATE_LIMIT_PER_MINUTE` | — | `20` | محدودیت دستور هر کاربر در دقیقه (۰ = خاموش) |
| `CONCURRENT_UPDATES` | — | `32` | آپدیت‌های هم‌زمان |
| `SESSION_TTL_SECONDS` | — | `3600` | عمر نشست `/search` |
| `POLLING_MAX_RUNTIME_SECONDS` | — | `0` (بی‌نهایت) | توقف نرم پس از N ثانیه (برای GitHub Actions) |

---

## نحوه‌ی اتصال به BeatAPI

آدرس پایه: `https://api.beatapi.io` — کلید فقط در هدر `Authorization: Bearer <BEATAPI_API_KEY>`
و فقط برای Run و وضعیت تسک فرستاده می‌شود.

| مرحله | درخواست | احراز هویت | خروجی مورد استفاده |
| --- | --- | --- | --- |
| ۱. Search | `POST /v1/capabilities/search` | ❌ | `data.data[]` (کارت‌ها)، `recommended`، `hints`، `groups` |
| ۲. Inspect | `POST /v1/capabilities/inspect` | ❌ | `input_schema`، `pricing`، `execution.mode`، `readiness`، `schema_hash` |
| ۳. Run | `POST /v1/capabilities/run` | ✅ | `output_text` یا تسک (`data.id` / `status`) |
| ۴. Poll | `GET /v1/tasks/{task_id}` | ✅ | `status` تا `succeeded`/`failed`، سپس `data.output.media[]` |

```bash
# 1) Search (keyless)
curl -sS -X POST https://api.beatapi.io/v1/capabilities/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"tiktok user profile"}'

# 2) Inspect (keyless)
curl -sS -X POST https://api.beatapi.io/v1/capabilities/inspect \
  -H 'Content-Type: application/json' \
  -d '{"reference":"data:tiktok.user.profile"}'

# 3) Run (Bearer required)
curl -sS -X POST https://api.beatapi.io/v1/capabilities/run \
  -H "Authorization: Bearer $BEATAPI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"reference":"data:tiktok.user.profile","input":{"username":"nasa"}}'

# 4) Poll an async task
curl -sS https://api.beatapi.io/v1/tasks/task_123 \
  -H "Authorization: Bearer $BEATAPI_API_KEY"
```

قواعد پیاده‌سازی‌شده:

- **هیچ reference ساخته نمی‌شود.** فقط مقادیر بازگشتی از Search/Inspect با الگوی
  `^(model|data|workflow):.+$` پذیرفته می‌شوند؛ هر چیز دیگری پیش از تماس با API رد می‌شود.
- **`error.retryable`** ملاک تصمیم است؛ برای `false` هیچ تلاش مجددی انجام نمی‌شود.
- **HTTP 429:** ابتدا `Retry-After` (هدر) و در نبود آن `error.retry_after_seconds` رعایت می‌شود؛
  اگر مقدار از سقف `BEATAPI_MAX_RETRY_AFTER_SECONDS` بیشتر باشد، تلاش متوقف و به کاربر اعلام می‌شود.
- **کلید API** در هیچ لاگ، پیام تلگرام یا URL دانلود فایل قرار نمی‌گیرد (فیلتر redaction در لاگ).
- **`idempotency_key`** برای هر اجرا یکتا تولید می‌شود تا تلاش مجدد باعث دوباره‌حساب‌شدن نشود.

---

## استقرار A: میزبانی ابری (Webhook)

برای استفاده‌ی همیشگی، حالت webhook را روی یک میزبان با HTTPS اجرا کنید. هر سه سرویس زیر
می‌توانند مستقیماً از همین ریپو با `Dockerfile` موجود دیپلوی کنند و با هر push به `main`
به‌روزرسانی خودکار انجام می‌دهند.

### Railway

1. **New Project → Deploy from GitHub repo** و انتخاب `gase99/Bot`.
2. در تب **Variables** این‌ها را اضافه کنید:
   ```text
   TELEGRAM_BOT_TOKEN=123456:ABC...
   BEATAPI_API_KEY=sk-...
   MODE=webhook
   LOG_LEVEL=INFO
   ```
3. در **Settings → Networking → Generate Domain** یک دامنه بگیرید (مثل
   `bot-production.up.railway.app`).
4. `WEBHOOK_URL=https://<همان-دامنه>/webhook` را اضافه کنید و سرویس را Redeploy کنید.
5. (اختیاری) در **Settings → Healthcheck Path** مقدار `/healthz` را بگذارید.
   Railway خودش متغیر `PORT` را تزریق می‌کند؛ نیازی به تنظیم دستی نیست.

### Render

1. **New → Web Service → Build and deploy from a Git repository** و انتخاب ریپو.
2. Runtime را **Docker** بگذارید (Render همان Dockerfile را build می‌کند).
3. در **Environment** مقادیر `TELEGRAM_BOT_TOKEN`، `BEATAPI_API_KEY`، `MODE=webhook` و بعد از
   ساخت سرویس `WEBHOOK_URL=https://<service>.onrender.com/webhook` را وارد کنید.
4. **Health Check Path** را `/healthz` بگذارید. Render متغیر `PORT` را خودش تعیین می‌کند.
5. ⚠️ سرویس‌های رایگان Render پس از بی‌کاری sleep می‌شوند و وب‌هوک را از دست می‌دهند؛ برای
   ربات ۲۴ ساعته از پلن paid یا حالت polling استفاده کنید.

### Fly.io

```bash
fly launch --no-deploy          # از Dockerfile موجود استفاده می‌کند
fly secrets set \
  TELEGRAM_BOT_TOKEN="123456:ABC..." \
  BEATAPI_API_KEY="sk-..." \
  MODE="webhook" \
  LOG_LEVEL="INFO"
fly deploy
fly domains list                # دامنه‌ی عمومی را بردارید
fly secrets set WEBHOOK_URL="https://<app>.fly.dev/webhook"
fly deploy
```

در `fly.toml` مطمئن شوید:

```toml
[http_service]
  internal_port = 8443
  force_https = true

[[http_service.checks]]
  path = "/healthz"
```

برنامه به `0.0.0.0:$PORT` گوش می‌دهد، پس پشت پروکسی Railway/Render/Fly بدون تنظیم اضافه کار می‌کند.

---

## استقرار B: GitHub Actions (Polling)

بدون هیچ میزبانی، ربات را از خود GitHub اجرا کنید. ورک‌فلوی
[`run-bot.yml`](.github/workflows/run-bot.yml) این کار را انجام می‌دهد.

### راه‌اندازی

1. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `BEATAPI_API_KEY`
2. (اختیاری) **Variables** → `LOG_LEVEL=INFO`.
3. تب **Actions** → **Run bot (polling)** → **Run workflow**.
4. تمام. ربات در حالت long polling اجرا می‌شود و به پیام‌ها جواب می‌دهد.

### محدودیت ۶ ساعته و زنجیره‌ی راه‌اندازی مجدد

GitHub هر job را حداکثر **۶ ساعت** اجرا می‌کند. این ریپو سه لایه محافظ دارد:

| مکانیزم | مقدار | نقش |
| --- | --- | --- |
| `POLLING_MAX_RUNTIME_SECONDS` | `19800` ثانیه (۵ ساعت و ۳۰ دقیقه) | ربات خودش با `stop_running()` تمیز خارج می‌شود |
| گام **Chain the next run** | `gh workflow run run-bot.yml` | فقط وقتی اجرای قبلی موفق (exit 0) بوده، اجرای بعدی را صف می‌کند |
| `schedule` | `55 */5 * * *` | تور نجات: اگر زنجیره پاره شد، تا ۵ ساعت و ۵۵ دقیقه بعد دوباره شروع می‌شود |
| `concurrency` | `telegram-bot-polling` | از اجرای هم‌زمان دو نمونه (خطای `Conflict: terminated by other getUpdates`) جلوگیری می‌کند |
| `timeout-minutes` | `350` | سقف سخت job، پایین‌تر از محدودیت ۶ ساعته |

نکته‌ها:

- `schedule` فقط روی **شاخه‌ی پیش‌فرض** (`main`) اجرا می‌شود و ممکن است با تأخیر چند دقیقه‌ای
  همراه باشد.
- در ریپوهای عمومی، ورک‌فلوهای زمان‌بندی‌شده پس از **۶۰ روز بی‌فعالیتی** غیرفعال می‌شوند؛
  یک commit یا اجرای دستی آن‌ها را برمی‌گرداند.
- برای self-dispatch به `permissions: actions: write` نیاز است (در فایل تنظیم شده).
- نشست‌ها در حافظه‌اند: با هر ری‌استارت، `/search` قبلی و تسک‌های در حال اجرا از دست می‌روند.
  برای کارهای طولانی، حالت میزبانی (A) مناسب‌تر است.
- اگر اجرا با خطا تمام شود، زنجیره عمداً متوقف می‌شود تا کانفیگ خراب در حلقه‌ی بی‌پایان نیفتد؛
  لاگ job را ببینید و مشکل را رفع کنید.

---

## استقرار C: لوکال و Docker

### اجرای مستقیم

```bash
make run           # حالت از فایل .env خوانده می‌شود
make run-polling   # اجبار به polling
make test          # تست‌ها
make lint          # ruff
```

### Docker

```bash
cp .env.example .env    # مقادیر را پر کنید
make docker-up          # docker compose up -d --build
make logs
make docker-down
```

یا بدون compose:

```bash
docker build -t telegram-beatapi-bot .
docker run --rm --env-file .env -p 8443:8443 telegram-beatapi-bot
```

برای تست webhook روی سیستم لوکال، یک تونل HTTPS بسازید و `WEBHOOK_URL` را روی آن بگذارید:

```bash
cloudflared tunnel --url http://localhost:8443
# یا: ngrok http 8443
# سپس در .env:
# WEBHOOK_URL=https://<tunnel-domain>/webhook
```

بررسی سلامت سرویس:

```bash
curl -s http://localhost:8443/healthz
# {"status":"ok","mode":"webhook"}
```

> در حالت `polling` سرور HTTP اجرا نمی‌شود؛ healthcheck داکر به‌صورت خودکار موفق است.

---

## تست و کیفیت کد

```bash
pip install -r requirements-dev.txt
ruff check .        # همان کاری که CI می‌کند
pytest -q           # ۴۳ تست، بدون نیاز به شبکه یا کلید
make cov            # گزارش پوشش
```

تست‌ها از `httpx.MockTransport` استفاده می‌کنند و این موارد را پوشش می‌دهند:

- پارس کردن Search/Inspect و نکشیدن کلید در درخواست‌های keyless
- ارسال `Authorization: Bearer` و `idempotency_key` یکتا در Run
- polling تا `succeeded`، خطای `failed` و timeout
- احترام به `Retry-After`، عدم تکرار خطاهای non-retryable و کران‌دار بودن retry
- رد کردن reference ساختگی پیش از تماس با API
- محدودیت حجم دانلود و عدم ارسال کلید به CDN
- اعتبارسنجی ورودی کاربر با JSON Schema و helperهای خروجی
- امنیت وب‌هوک (توکن مخفی اشتباه → ۴۰۱) و اعتبارسنجی تنظیمات

CI (`.github/workflows/ci.yml`) روی هر push/PR به `main` اجرا می‌شود: نصب وابستگی‌ها →
`ruff check .` → `pytest -q` → smoke import → build تصویر Docker.

---

## نکات امنیتی

> **کلید BeatAPI مانند رمز عبور است.** آن را فقط در Secrets میزبان یا GitHub نگه دارید.

- `.env` در `.gitignore` است و هرگز commit نمی‌شود (`!.env.example` استثناست).
- توکن‌ها با `SecretStr` نگهداری می‌شوند؛ `repr(settings)` مقدار آن‌ها را ماسک می‌کند.
- یک `logging.Filter` و یک `Formatter` اختصاصی، هر توکن شناخته‌شده را — حتی داخل traceback —
  پیش از نوشتن لاگ با `***` جایگزین می‌کنند.
- پیام‌های خطای تلگرام فقط پیام کوتاه و کد خطا را نشان می‌دهند؛ stack trace فقط در لاگ سرور است.
- نقطه‌ی پایانی وب‌هوک با `X-Telegram-Bot-Api-Secret-Token` (مقایسه‌ی constant-time) محافظت
  می‌شود و بدنه‌های بزرگ‌تر از ۱ مگابایت رد می‌شوند. اگر `WEBHOOK_SECRET_TOKEN` خالی باشد،
  از توکن ربات مشتق می‌شود.
- دانلود فایل‌های نتیجه با همان کلاینت ولی **بدون هدر Authorization** انجام می‌شود تا کلید به CDN
  یا دامنه‌ی شخص ثالث نرسد؛ الگوی URL نیز فقط `http(s)` است.
- برای ربات خصوصی، `ALLOWED_USER_IDS` را تنظیم کنید (شناسه‌ی خود را از `@userinfobot` بگیرید) و
  `RATE_LIMIT_PER_MINUTE` را کم کنید.
- مستندات عمومی (Swagger) در حالت webhook غیرفعال است.
- در صورت لو رفتن کلید: کلید را در داشبورد BeatAPI حذف/چرخش دهید و توکن ربات را در BotFather
  (`/revoke`) عوض کنید.
- محتوای بازگشتی از API را داده‌ی غیرقابل‌اعتماد بدانید؛ ربات آن را فقط نمایش می‌دهد و هرگز
  دستورهای داخل آن را اجرا نمی‌کند.

---

## عیب‌یابی

| نشانه | علت احتمالی | راه‌حل |
| --- | --- | --- |
| `Configuration is invalid` هنگام شروع | `.env` ناقص است | `cp .env.example .env` و پر کردن توکن‌ها؛ در وب‌هوک `WEBHOOK_URL` الزامی است |
| `WEBHOOK_URL must use https for public hosts` | آدرس http غیرلوکال | از HTTPS استفاده کنید (یا `MODE=polling`) |
| ربات در حالت webhook جواب نمی‌دهد | دامنه/مسیر اشتباه یا سرویس خواب | `curl https://<domain>/healthz`؛ مسیر باید همان `WEBHOOK_URL` باشد |
| `Rejected a webhook call with an invalid secret token` | تلگرام با secret دیگری ثبت شده | ربات را دوباره دیپلوی کنید تا `set_webhook` اجرا شود |
| `Conflict: terminated by other getUpdates request` | دو نمونه polling هم‌زمان | یکی را متوقف کنید؛ در Actions به `concurrency` دست نزنید |
| `کلید BeatAPI پذیرفته نشد` (۴۰۱) | کلید اشتباه/غیرفعال | کلید را در داشبورد بررسی کنید |
| `اعتبار حساب BeatAPI کافی نیست` (۴۰۲) | موجودی کم | [شارژ حساب](https://beatapi.io/dashboard/billing) |
| `محدودیت نرخ درخواست` (۴۲۹) | rate limit | چند لحظه صبر؛ مقدار `Retry-After` رعایت می‌شود |
| `زمان انتظار به پایان رسید` | تسک طولانی‌تر از `POLL_TIMEOUT_SECONDS` | مقدار را بالا ببرید (مثلاً ۱۸۰۰) |
| فایل ارسال نشد | حجم > ۵۰MB تلگرام | `MAX_DOWNLOAD_BYTES` را کم کنید یا از لینک نتیجه استفاده کنید |
| ورک‌فلوی زمان‌بندی اجرا نمی‌شود | شاخه‌ی غیرپیش‌فرض یا ۶۰ روز بی‌فعالیتی | روی `main` مرج کنید یا دستی اجرا کنید |
| Docker healthcheck در polling قرمز است | (نباید رخ دهد) | با `docker logs` بررسی کنید؛ در polling healthcheck خودکار سبز است |

راهنمای رسمی خطاها: <https://docs.beatapi.io/errors> — قابلیت‌ها: <https://docs.beatapi.io/capabilities>.

---

## English summary

A production-ready Telegram bot for [BeatAPI](https://beatapi.io). It implements the
**Search → Inspect → Run** loop with an async `httpx` client, validates user JSON against the
capability's `input_schema` before spending any balance, polls async tasks with jittered 5–10 s
intervals, and always delivers the actual result — text, link or downloaded file.

- **Three ways to run:** hosted webhook (`MODE=webhook`, Railway/Render/Fly), GitHub Actions long
  polling (`MODE=polling`, 6-hour job limit handled with a graceful 5.5 h stop + self re-dispatch),
  or locally with Docker/Make.
- **Security:** the BeatAPI key lives server-side only, is redacted from logs, never reaches
  Telegram or media CDNs, webhook calls are authenticated with a secret token, and users can be
  restricted with `ALLOWED_USER_IDS`.
- **Reliability:** `error.retryable=false` is never retried, 429 honours `Retry-After`, retries use
  exponential backoff with jitter, every run carries an `idempotency_key`, and references are never
  invented.
- **Quality:** typed async code, modular layout, structured logging, 43 offline unit tests with
  `httpx.MockTransport`, ruff lint, and CI that also builds the Docker image.

Quick start: `git clone`, `cp .env.example .env`, fill `TELEGRAM_BOT_TOKEN` and
`BEATAPI_API_KEY`, then `MODE=polling python -m app.main` (or deploy the Dockerfile).

---

## مجوز

[MIT](LICENSE) © 2026 gase99
