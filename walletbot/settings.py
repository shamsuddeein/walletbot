"""
Django settings for walletbot project.
"""
import os
from pathlib import Path
from decouple import config, Csv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security ──────────────────────────────────────────────────
SECRET_KEY = config("SECRET_KEY")
DEBUG = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=Csv())
CSRF_TRUSTED_ORIGINS = [
    "https://*.up.railway.app",
    "https://*.ngrok-free.app",
    "https://*.ngrok-free.dev",
]

# ── Apps ──────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "tracker",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "walletbot.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "walletbot.wsgi.application"

# ── Database ──────────────────────────────────────────────────
DATABASES = {
    "default": dj_database_url.config(
        default=config("DATABASE_URL", default="postgres://walletbot:walletbot@localhost:5432/walletbot"),
        conn_max_age=600,
    )
}

# ── Static files ──────────────────────────────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Celery ────────────────────────────────────────────────────
CELERY_BROKER_URL = config("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = config("REDIS_URL", default="redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"

from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    "daily-digest": {
        "task": "tracker.tasks.daily_digest",
        "schedule": crontab(hour=9, minute=0),
    },
    "wallet-anomaly-check": {
        "task": "tracker.tasks.wallet_anomaly_check",
        "schedule": 3600,  # every hour
    },
}

# ── App-specific settings ─────────────────────────────────────
HELIUS_API_KEY = config("HELIUS_API_KEY", default="")
HELIUS_WEBHOOK_ID = config("HELIUS_WEBHOOK_ID", default="")
HELIUS_WEBHOOK_SECRET = config("HELIUS_WEBHOOK_SECRET", default="")

TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_ALLOWED_USER_ID = config("TELEGRAM_ALLOWED_USER_ID", default="0", cast=int)

OPENROUTER_API_KEY = config("OPENROUTER_API_KEY", default="")

WEBHOOK_BASE_URL = config("WEBHOOK_BASE_URL", default="http://localhost:8000")

# Matching thresholds (0–100)
NAME_MATCH_THRESHOLD = config("NAME_MATCH_THRESHOLD", default=75, cast=int)
SYMBOL_MATCH_THRESHOLD = config("SYMBOL_MATCH_THRESHOLD", default=80, cast=int)
LOGO_MATCH_THRESHOLD = config("LOGO_MATCH_THRESHOLD", default=10, cast=int)  # imagehash distance ≤ this

MAX_WALLETS = 10

# Backfill settings
BACKFILL_DAYS = config("BACKFILL_DAYS", default=30, cast=int)
BACKFILL_MAX_TRANSACTIONS = config("BACKFILL_MAX_TRANSACTIONS", default=200, cast=int)

# Authentication Backends
AUTHENTICATION_BACKENDS = [
    "tracker.backends.EmailOrUsernameBackend",
    "django.contrib.auth.backends.ModelBackend",
]
