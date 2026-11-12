"""
Development settings for KiliLedger Pro — uses SQLite as confirmed for local development.
"""
from .base import *  # noqa: F403, F401

import os
from environs import Env
env = Env()
env.read_env()
# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = env("EMAIL_ID")
EMAIL_HOST_PASSWORD = env("EMAIL_PASSWORD")






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





