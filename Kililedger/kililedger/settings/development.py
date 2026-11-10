"""
Development settings for KiliLedger Pro — uses SQLite as confirmed for local development.
"""
from .base import *  # noqa: F403, F401

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "*"]

# Confirmed SQLite Database for Local Development
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Remove WhiteNoise in dev if not installed locally, or keep standard staticfiles
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m.lower()]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
