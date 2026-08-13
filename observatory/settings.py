"""Django settings for the observatory project.

Configuration comes from environment variables, loaded from a .env file that is
never committed. The defaults are the local development values, so running
locally needs no environment at all, while the deployed instance overrides
everything through its own .env.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------- core
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "True") == "True"

ALLOWED_HOSTS = [h.strip() for h in os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# Only hostnames belong here, not bare IP addresses - Django rejects an origin
# without a scheme, and a raw IP cannot hold a certificate anyway.
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS
    if h not in ("localhost", "127.0.0.1") and not h.replace(".", "").isdigit()
]


# --------------------------------------------------------------------- apps
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "reliability",
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

ROOT_URLCONF = "observatory.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "observatory.wsgi.application"


# ----------------------------------------------------------------- database
DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.environ.get("GTFSOBS_DB_NAME", "gtfsobs"),
        "USER": os.environ.get("GTFSOBS_DB_USER", "gtfsobs"),
        "PASSWORD": os.environ.get("GTFSOBS_DB_PASSWORD", "gtfsobs"),
        "HOST": os.environ.get("GTFSOBS_DB_HOST", "localhost"),
        "PORT": os.environ.get("GTFSOBS_DB_PORT", "5432"),
        # The harvester opens a connection every poll. Reusing it avoids a
        # handshake per minute for the life of the process.
        "CONN_MAX_AGE": 60,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --------------------------------------------------------------------- i18n
LANGUAGE_CODE = "en-au"
TIME_ZONE = "Australia/Sydney"
USE_I18N = True
# Timestamps are stored in UTC and converted for display. Every analysis module
# sets the Sydney zone explicitly rather than relying on this, because the
# database server timezone is not guaranteed to match the application's.
USE_TZ = True


# ------------------------------------------------------------------- static
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}


# ----------------------------------------------------------------- security
# Caddy terminates TLS and proxies to Gunicorn over localhost, so Django needs
# to be told the original scheme or it will treat every request as insecure.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"


# ------------------------------------------------------------------ logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # Query logging at DEBUG would emit a line per row during the GTFS load.
        "django.db.backends": {"level": "WARNING", "propagate": False,
                               "handlers": ["console"]},
    },
}
