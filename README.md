# Telegram Campaign Manager

سامانه ناهمگام مدیریت کمپین تلگرام برای ارسال مجاز و زمان‌بندی‌شده به گروه‌هایی که مالک حساب از قبل عضو آن‌هاست. پنل فارسی با **Bot API** فقط مدیریت می‌کند؛ همه پیام‌های کمپین منحصراً با حساب کاربر و **Telethon/MTProto** فرستاده می‌شوند.

## معماری و تضمین‌های ایمنی

`Admin → aiogram bot → SQLAlchemy/SQLite → APScheduler → Telethon account → allowlisted chats`

هر کمپین دقیقاً یک حساب دارد. گروه باید صریحاً برای همان حساب ثبت و فعال شده باشد. ارسال ترتیبی و با تأخیر تصادفی محافظه‌کارانه انجام می‌شود؛ گردش یا جایگزینی خودکار حساب، کشف گروه، دورزدن محدودیت، یا ارسال موازی وجود ندارد. `FloodWait` و `SlowModeWait` به مدت اعلام‌شده تلگرام متوقف می‌شوند و از حساب دیگری استفاده نمی‌شود. خطای یک گروه ثبت شده و مانع بررسی گروه بعدی نیست (جز wait که اجرای جاری را متوقف می‌کند).

اجزا: `app/bot` پنل و مجوز مرکزی؛ `app/telegram` مدیریت singleton کلاینت‌ها؛ `app/campaigns` rotation و scheduler؛ `app/services` اجرا و delivery log؛ `app/database` مدل‌ها و WAL؛ `app/cli` احراز هویت و مشاهده dialogها.

## نیازمندی‌ها و آماده‌سازی اعتبارنامه

Python 3.12+ یا Docker Compose، یک VPS اوبونتو با ساعت صحیح، و حساب‌هایی که اجازه ارسال به گروه مقصد دارند لازم است.

1. در گفتگوی `@BotFather` دستور `/newbot` را اجرا و token پنل را بگیرید. bot را به گروه مقصد اضافه نکنید؛ bot تبلیغ نمی‌فرستد.
2. در `https://my.telegram.org` بخش API Development، `api_id` و `api_hash` را بسازید.
3. شناسه عددی مدیران را با ویرگول در `ADMIN_IDS` قرار دهید.

```bash
cp .env.example .env
nano .env
```

متغیرهای الزامی: `BOT_TOKEN`, `ADMIN_IDS`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `DATABASE_URL`. متغیرهای عملیاتی: `TIMEZONE`, `SESSION_DIRECTORY`, `MEDIA_DIRECTORY`, `LOG_LEVEL`, `MIN_SEND_DELAY_SECONDS`, `MAX_SEND_DELAY_SECONDS`, `CATCH_UP_MISSED_RUNS`, `ALERT_COOLDOWN_SECONDS`. برای local مقدار `DATABASE_URL=sqlite+aiosqlite:///./data/app.db` و مسیرهای `./data/sessions` و `./data/media` مناسب‌اند.

## نصب و اجرای محلی

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
mkdir -p data/sessions data/media && chmod 700 data/sessions
python -m app.cli.login_account
python -m app.cli.list_dialogs --account account_main
python -m app.main
```

CLI ابتدا label، سپس شماره، OTP و در صورت نیاز رمز دومرحله‌ای را به شکل hidden می‌گیرد. این اسرار در DB/log ذخیره نمی‌شوند. تکراری بودن label و Telegram user ID کنترل می‌شود. برای حساب اضافی همان `login_account` را با label جدید اجرا کنید.

`list_dialogs` فقط گروه/کانال‌های موجود را چاپ می‌کند و **هیچ‌کدام را ثبت نمی‌کند**. در پنل «👥 گروه‌ها» حساب را انتخاب، گفتگوها را صفحه‌بندی و هر مقصد مجاز را صریحاً ثبت کنید. wizard «➕ کمپین جدید» نام، حساب، فاصله، چند گروه، متن یا عکس، پیش‌نمایش و تأیید دارد. جزئیات کمپین همه عملیات مدیریت، پیام‌ها، گروه‌ها و ارسال دستی را ارائه می‌دهد؛ گروه‌ها هنگام تغییر حساب هرگز خودکار منتقل نمی‌شوند.

## Docker و اولین ورود

```bash
docker compose build
docker compose run --rm app python -m app.cli.login_account
docker compose run --rm app python -m app.cli.list_dialogs --account account_main
docker compose up -d
```

ورود تعاملی در container موقت است اما فایل session و DB در volumeهای دائمی باقی می‌مانند. عملیات روزانه:

```bash
docker compose up -d --build        # start/rebuild
docker compose stop                 # stop
docker compose restart app          # restart
docker compose logs -f --tail=200 app
docker compose down                 # containers only; volumes remain
```

## زمان‌بندی، اجرای دستی و گزارش

زمان‌ها در UTC ذخیره و در timezone تنظیم‌شده نمایش داده می‌شوند. در startup کمپین‌های فعال بازیابی می‌شوند. اگر موعد گذشته باشد و `CATCH_UP_MISSED_RUNS=true`، حداکثر یک اجرای catch-up انجام می‌شود؛ intervalهای از دست‌رفته replay نمی‌شوند. اگر false باشد، اولین موعد آینده محاسبه می‌شود. claim اتمیک DB از اجرای تکراری جلوگیری می‌کند و claim یک‌ساعته stale قابل بازیابی است. rotation index پس از اجرا persist و پس از restart ادامه می‌یابد.

اجرای دستی کمپین rotation را جلو می‌برد ولی `next_run_at` را تغییر نمی‌دهد. test-send مستقل باید مستقیماً به یک TargetChat فعال فرستاده شود و schedule/rotation را تغییر نمی‌دهد. DeliveryLog موفق/ناموفق/retry/skipped، شناسه پیام و خطای کوتاه را نگه می‌دارد. پنل گزارش کلی، وضعیت حساب و فهرست کمپین‌ها را نشان می‌دهد.

## Backup، restore و update

برای سازگاری WAL ابتدا app را متوقف کنید. sessionها بسیار حساس‌اند و backup باید رمزگذاری و محدود شود.

```bash
docker compose stop app
mkdir -p backups
docker run --rm -v telegram-campaing-manager_database:/src:ro -v "$PWD/backups":/dst alpine tar czf /dst/database.tgz -C /src .
docker run --rm -v telegram-campaing-manager_sessions:/src:ro -v "$PWD/backups":/dst alpine tar czf /dst/sessions.tgz -C /src .
docker compose start app
# restore (روی نصب متوقف‌شده): tar archive را در volume متناظر استخراج کنید
git pull --ff-only && docker compose up -d --build
```

نام volume را با `docker volume ls` تأیید کنید. پیش از update از DB و sessions نسخه پشتیبان بگیرید.

## امنیت session و VPS اوبونتو

**دارنده فایل Telethon session ممکن است به حساب تلگرام دسترسی کامل پیدا کند.** آن را چاپ، ارسال، commit، در bot نمایش، یا در backup بدون رمزگذاری نگهداری نکنید. directory با mode `0700` ساخته می‌شود، process در Docker کاربر non-root است، و session/database/media volume جدا دارند. `.env` و sessionها gitignored هستند. دسترسی SSH کلیدی، firewall (پنل polling است و port ورودی لازم ندارد)، disk encryption، update امنیتی و backup رمزگذاری‌شده توصیه می‌شود.

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"   # سپس login مجدد
git clone YOUR_REPOSITORY_URL telegram-campaign-manager
cd telegram-campaign-manager && cp .env.example .env && nano .env
docker compose build
docker compose run --rm app python -m app.cli.login_account
docker compose up -d
```

## عیب‌یابی

* `BOT_TOKEN is required`: `.env` و env_file را بررسی کنید.
* authorization expired: container را stop، CLI login را اجرا و سپس start کنید؛ هرگز session را در bot نفرستید.
* `database is locked`: فقط یک instance اجرا کنید؛ WAL و busy timeout فعال‌اند.
* `FloodWait`: اجرای ناقص rotation را جلو نمی‌برد؛ موعد یک retry روشن بعد از زمان اعلام‌شده ثبت می‌شود. حساب جایگزین یا retry بی‌نهایت وجود ندارد.
* write forbidden/private/banned: مجوز و عضویت حساب انتخابی را بررسی و target نامعتبر را غیرفعال کنید.
* تست: `pytest -q`؛ lint: `ruff check .`؛ import: `python -c 'import app.main'`.

## مدل داده و migrations

`SenderAccount`, `TargetChat`, `Campaign`, `CampaignTarget`, `CampaignMessage`, `DeliveryLog` با foreign key، uniqueness و indexهای گزارش ساخته می‌شوند. OTP/password/phone در مدل وجود ندارند. `create_all` در اولین startup خودکار است. برای نسخه نخست Alembic عمداً افزوده نشده تا deployment اولیه تک‌فایلی مطمئن بماند؛ پیش از هر تغییر schema در production باید Alembic baseline و migration اضافه شود. مدل‌های SQLAlchemy برای مهاجرت بعدی به PostgreSQL طراحی شده‌اند.

## محدودیت‌ها و گام بعد

این release برای یک instance و SQLite طراحی شده و migration نسخه‌ای Alembic، PostgreSQL، webhook و داشبورد وب ندارد. گزارش پنل فعلاً تجمعی است و فیلتر صفحه‌بندی‌شده روز/خطا ندارد. APScheduler job store حافظه‌ای است اما `next_run_at` پایگاه‌داده منبع حقیقت و در startup بازسازی می‌شود. پیش از production تست staging، Alembic، مانیتورینگ، تست restore و بازبینی مجوز volume توصیه می‌شود. هیچ credential واقعی همراه repository نیست.

موارد صریح باقی‌مانده از دامنه اولیه: Alembic «اختیاری» بود و عمداً وجود ندارد؛ backend فقط SQLite تک-instance است؛ VIDEO/DOCUMENT طبق درخواست فقط توسعه‌پذیرند و هنوز پشتیبانی نمی‌شوند؛ گزارش امروز/خطا/حساب موجود است اما pagination و خروجی فایل ندارد؛ alertها برای خطای اتصال، rate limit، خطاهای تحویل و خطای پنل هستند ولی سامانه مانیتورینگ بیرونی جایگزین نمی‌کنند. سایر workflowهای درخواستی پنل، CLI، allowlist، scheduler، delivery، Docker و آزمون‌ها پیاده‌سازی شده‌اند.
