"""
Base Django settings for KiliLedger Pro (Kilimall Seller Financial Management & Analytics SaaS).
"""
import os
from decimal import Decimal
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-kililedger-dev-key-change-in-production-9f8e7d6c5b4a",
)

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # KiliLedger Pro Modular Apps
    "apps.accounts.apps.AccountsConfig",
    "apps.stores.apps.StoresConfig",
    "apps.statements.apps.StatementsConfig",
    "apps.orders.apps.OrdersConfig",
    "apps.products.apps.ProductsConfig",
    "apps.analytics.apps.AnalyticsConfig",
    "apps.reports.apps.ReportsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.TenantIsolationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kililedger.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.global_workspace_context",
            ],
        },
    },
]

WSGI_APPLICATION = "kililedger.wsgi.application"
ASGI_APPLICATION = "kililedger.asgi.application"

# Custom User Model for Multi-Vendor RBAC
AUTH_USER_MODEL = "accounts.VendorUser"

# Authentication Redirects
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "analytics:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization (Kenya Default)
LANGUAGE_CODE = "en-ke"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

# Static & Media files
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# File Upload Limits (10 MB max for Kilimall Excel statements)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# KiliLedger Domain Constants (Confirmed Business Baselines)
KILILEDGER_DEFAULTS = {
    "DEFAULT_ACCOUNTING_BASIS": "CASH_FLOW",          # Approved: Settlement Cash-Flow Basis default on SCR-03
    "DEFAULT_PENDING_AGING_DAYS": 14,                 # Approved: 14-day threshold before Overdue Pending
    "CRITICAL_PENDING_AGING_DAYS": 21,                # Critical courier return escalation window
    "DEFAULT_TARGET_NET_MARGIN_PCT": Decimal("15.00"),
    "PACKAGE_SIZE_ANOMALY_RATIO": Decimal("0.35"),    # Flag when DS fee exceeds 35% of product price
    "CURRENCY_CODE": "KES",
}
