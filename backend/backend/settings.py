"""
Django settings for tg-event-analytics.

A configurable framework for analyzing Telegram posts into deduplicated events.
Reuses the Telegram account-manager pattern from llm-council.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")

# When served behind a TLS-terminating reverse proxy (e.g. nginx), trust the
# forwarded scheme so request.is_secure() / CSRF / secure cookies work.
if os.getenv("DJANGO_SECURE_PROXY_SSL_HEADER", "true").lower() == "true":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Origins allowed for unsafe (POST) requests over HTTPS — required for the admin
# login form when accessed via a proxied https:// host. Comma-separated, each
# value must include the scheme, e.g. "https://analytics.example.com".
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "rangefilter",
    "accounts",
    "analysis",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "backend.wsgi.application"

# Database: Postgres if configured, else SQLite for dev.
if os.getenv("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB"),
            "USER": os.getenv("POSTGRES_USER", "postgres"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "uk"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
if not DEBUG:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
}

CORS_ALLOW_ALL_ORIGINS = DEBUG

# --- External services ---
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", os.getenv("TG_API_ID", ""))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", os.getenv("TG_API_HASH", ""))

TELEZIP_API_KEY = os.getenv("TELEZIP_API_KEY", "")
TELEZIP_BASE_URL = os.getenv("TELEZIP_BASE_URL", "https://api.telezip.net/v3")
# Max CONCURRENT TeleZip requests (API allows very few). Enforced in TelezipClient.
TELEZIP_MAX_CONCURRENCY = int(os.getenv("TELEZIP_MAX_CONCURRENCY", "2"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_BASE_URL = os.getenv("OPENROUTER_API_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-2.5-flash")

# --- Logging ---
# Stage workers are long-lived; their per-tick progress goes through the
# `analysis` logger at INFO (mon_filter/mon_prescreen/mon_tag/collect counts).
# Default Django logging surfaces only WARNING+, so without this the worker
# stdout shows just "старт" + TeleZip retries. Level overridable via LOG_LEVEL.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "ts": {
            "format": "%(asctime)s %(levelname)s %(name)s | %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "ts"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "analysis": {
            "handlers": ["console"],
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
