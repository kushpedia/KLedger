from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path

urlpatterns = [
    path("django-admin/", admin.site.urls),
    # Root redirects to login or dashboard
    path("", lambda r: redirect("analytics:dashboard" if r.user.is_authenticated else "accounts:login")),
    # Top-level direct shortcuts matching SCR-01 & email activation links
    path("", include("apps.accounts.urls")),
    # App-specific URL namespaces
    path("stores/", include("apps.stores.urls", namespace="stores")),
    path("statements/", include("apps.statements.urls", namespace="statements")),
    path("orders/", include("apps.orders.urls", namespace="orders")),
    path("products/", include("apps.products.urls", namespace="products")),
    path("dashboard/", include("apps.analytics.urls", namespace="analytics")),
    path("reports/", include("apps.reports.urls", namespace="reports")),
]
