"""
URL routes for `apps.accounts` (SCR-01 Auth Portal, SCR-15 Settings & Notifications, SCR-16 Admin Console).
"""
from django.urls import path
from apps.accounts.views import (
    AuthSplitPortalView,
    NotificationsDrawerPartialView,
    PlatformAdminConsoleView,
    VendorLogoutView,
    VendorSettingsAndRulesView,
)

app_name = "accounts"

urlpatterns = [
    path("login/", AuthSplitPortalView.as_view(), name="login"),
    path("register/", AuthSplitPortalView.as_view(), {"tab": "register"}, name="register"),
    path("logout/", VendorLogoutView.as_view(), name="logout"),
    path("settings/", VendorSettingsAndRulesView.as_view(), name="settings_rules"),
    path("notifications/drawer/", NotificationsDrawerPartialView.as_view(), name="notifications_drawer"),
    path("platform-admin/", PlatformAdminConsoleView.as_view(), name="platform_admin_console"),
]
