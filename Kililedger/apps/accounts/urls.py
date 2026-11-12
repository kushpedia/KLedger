from django.urls import path
from apps.accounts.views import (
    ActivateAccountView,
    AuthSplitPortalView,
    NotificationsDrawerPartialView,
    PlatformAdminConsoleView,
    ResendActivationLinkView,
    VendorLogoutView,
    VendorSettingsAndRulesView,
)

app_name = "accounts"

urlpatterns = [
    path("login/", AuthSplitPortalView.as_view(), name="login"),
    path("logout/", VendorLogoutView.as_view(), name="logout"),
    path("activate/<str:token>/", ActivateAccountView.as_view(), name="activate_account"),
    path("activate-resend/", ResendActivationLinkView.as_view(), name="resend_activation"),
    path("settings/", VendorSettingsAndRulesView.as_view(), name="settings_rules"),
    path("notifications/drawer/", NotificationsDrawerPartialView.as_view(), name="notifications_drawer"),
    path("platform-admin/", PlatformAdminConsoleView.as_view(), name="platform_admin_console"),
]
