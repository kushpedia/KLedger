"""
Root URL configuration for KiliLedger Pro.
"""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("", RedirectView.as_view(pattern_name="analytics:dashboard", permanent=False), name="root"),
    path("", include("apps.accounts.urls", namespace="accounts")),
    path("stores/", include("apps.stores.urls", namespace="stores")),
    path("statements/", include("apps.statements.urls", namespace="statements")),
    path("orders/", include("apps.orders.urls", namespace="orders")),
    path("products/", include("apps.products.urls", namespace="products")),
    path("dashboard/", include("apps.analytics.urls", namespace="analytics")),
    path("reports/", include("apps.reports.urls", namespace="reports")),
]
