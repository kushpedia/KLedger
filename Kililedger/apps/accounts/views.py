"""
Views for Authentication (SCR-01), Vendor Settings & Reconciliation Rules (SCR-15),
Slide-Over Notifications Drawer (SCR-15), and Platform Administration Console (SCR-16).
"""
from django.contrib import messages
from django.contrib.auth import login, logout
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.forms import (
    ReconciliationRulesForm,
    StaffMemberInviteForm,
    VendorLoginForm,
    VendorRegistrationForm,
)
from apps.accounts.middleware import PlatformAdminRequiredMixin, VendorRequiredMixin
from apps.accounts.models import ParserSchemaDiagnosticLog, VendorNotification, VendorTenant, VendorUser


class AuthSplitPortalView(View):
    """
    Implements SCR-01: Split-Screen Authentication & Vendor Registration Portal.
    Supports switching between 'Sign In' and 'Create Vendor Account' tabs.
    """

    template_name = "accounts/auth_split.html"

    def get(self, request):
        if request.user.is_authenticated:
            if request.user.is_platform_operator and not request.user.tenant_id:
                return redirect("accounts:platform_admin_console")
            return redirect("analytics:dashboard")

        active_tab = request.GET.get("tab", "login")
        return render(
            request,
            self.template_name,
            {
                "active_tab": active_tab,
                "login_form": VendorLoginForm(request),
                "register_form": VendorRegistrationForm(),
            },
        )

    def post(self, request):
        action = request.POST.get("auth_action", "login")

        if action == "register":
            register_form = VendorRegistrationForm(request.POST)
            login_form = VendorLoginForm(request)
            if register_form.is_valid():
                user = register_form.save()
                login(request, user)
                messages.success(
                    request,
                    f"Welcome to KiliLedger Pro, {user.tenant.business_name}! Let's connect your first Kilimall store.",
                )
                return redirect("stores:onboarding_wizard")
            return render(
                request,
                self.template_name,
                {"active_tab": "register", "login_form": login_form, "register_form": register_form},
            )

        # Default: Sign In action
        login_form = VendorLoginForm(request, data=request.POST)
        register_form = VendorRegistrationForm()
        if login_form.is_valid():
            user = login_form.get_user()
            login(request, user)
            if user.is_platform_operator and not user.tenant_id:
                return redirect("accounts:platform_admin_console")
            return redirect("analytics:dashboard")

        return render(
            request,
            self.template_name,
            {"active_tab": "login", "login_form": login_form, "register_form": register_form},
        )


class VendorLogoutView(View):
    """Logs out the current user and clears session store context."""

    def post(self, request):
        logout(request)
        return redirect("accounts:login")

    def get(self, request):
        logout(request)
        return redirect("accounts:login")


class VendorSettingsAndRulesView(VendorRequiredMixin, View):
    """
    Implements SCR-15: Vendor Settings, Reconciliation Thresholds, Team Sub-Roles,
    and Temporary Admin Support Access Grant.
    """

    template_name = "accounts/settings_rules.html"
    allowed_roles = (VendorUser.ROLE_OWNER, VendorUser.ROLE_FINANCE)

    def get(self, request):
        active_tab = request.GET.get("tab", "rules")
        rules_form = ReconciliationRulesForm(instance=request.tenant)
        invite_form = StaffMemberInviteForm()
        team_members = request.tenant.users.all().order_by("date_joined")
        return render(
            request,
            self.template_name,
            {
                "active_tab": active_tab,
                "rules_form": rules_form,
                "invite_form": invite_form,
                "team_members": team_members,
            },
        )

    def post(self, request):
        form_type = request.POST.get("form_type", "rules")

        if form_type == "support_access" and request.user.can_manage_stores_and_rollback:
            toggle_action = request.POST.get("support_action")
            if toggle_action == "grant":
                request.tenant.grant_temporary_support_access(hours=24)
                messages.info(request, "Temporary 24-hour read-only support access granted to KiliLedger Support.")
            else:
                request.tenant.revoke_support_access()
                messages.success(request, "Strict Privacy Lock restored. Support access revoked immediately.")
            return redirect("accounts:settings_rules")

        if form_type == "invite_staff" and request.user.can_manage_stores_and_rollback:
            invite_form = StaffMemberInviteForm(request.POST)
            if invite_form.is_valid():
                email = invite_form.cleaned_data["email"].strip().lower()
                if VendorUser.objects.filter(email__iexact=email).exists():
                    messages.error(request, "A user with that email already exists.")
                else:
                    VendorUser.objects.create_user(
                        username=email,
                        email=email,
                        password=invite_form.cleaned_data["temporary_password"],
                        first_name=invite_form.cleaned_data["full_name"].strip(),
                        tenant=request.tenant,
                        role=invite_form.cleaned_data["role"],
                    )
                    messages.success(request, f"Team member {email} added to your workspace.")
                return redirect("accounts:settings_rules")

        # Default: Save Reconciliation & Profit Rules
        rules_form = ReconciliationRulesForm(request.POST, instance=request.tenant)
        if rules_form.is_valid():
            tenant = rules_form.save()
            request.session["active_accounting_basis"] = tenant.default_accounting_basis
            messages.success(
                request,
                f"Reconciliation rules saved. Pending orders will flag as Overdue after {tenant.pending_aging_threshold_days} days.",
            )
            return redirect("accounts:settings_rules")

        return render(
            request,
            self.template_name,
            {
                "active_tab": "rules",
                "rules_form": rules_form,
                "invite_form": StaffMemberInviteForm(),
                "team_members": request.tenant.users.all(),
            },
        )


class NotificationsDrawerPartialView(VendorRequiredMixin, View):
    """
    Implements SCR-15 Slide-Over Notifications Drawer (loaded via HTMX into `#drawer-container`).
    """

    template_name = "components/_slide_over_drawer.html"

    def get(self, request):
        category_filter = request.GET.get("category", "all")
        qs = VendorNotification.objects.filter(tenant=request.tenant)
        if category_filter != "all":
            qs = qs.filter(category=category_filter)
        return render(
            request,
            self.template_name,
            {
                "notifications": qs[:25],
                "category_filter": category_filter,
            },
        )

    def post(self, request):
        # Mark all notifications as read
        VendorNotification.objects.filter(tenant=request.tenant, is_read=False).update(is_read=True)
        if request.headers.get("HX-Request"):
            return self.get(request)
        return redirect(request.META.get("HTTP_REFERER", "analytics:dashboard"))


class PlatformAdminConsoleView(PlatformAdminRequiredMixin, TemplateView):
    """
    Implements SCR-16: Platform Administration & Parser Health Console.
    Strictly enforces tenant financial privacy (shows metadata counts and anonymized parser logs only).
    """

    template_name = "accounts/admin_console.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenants = VendorTenant.objects.annotate(user_count=Count("users", distinct=True))
        all_logs = ParserSchemaDiagnosticLog.objects.all()

        context.update(
            {
                "tenants": tenants,
                "total_tenants": tenants.count(),
                "active_tenants": tenants.filter(is_active=True).count(),
                "diagnostic_logs": all_logs[:30],
                "flagged_logs_count": all_logs.filter(auto_resolved=False).count(),
            }
        )
        return context

    def post(self, request):
        tenant_id = request.POST.get("tenant_id")
        action = request.POST.get("admin_action")
        if tenant_id and action in ("suspend", "activate"):
            tenant = get_object_or_404(VendorTenant, id=tenant_id)
            tenant.is_active = action == "activate"
            tenant.save(update_fields=["is_active", "updated_at"])
            status_label = "activated" if tenant.is_active else "suspended"
            messages.success(request, f"Tenant '{tenant.business_name}' has been {status_label}.")
        return redirect("accounts:platform_admin_console")
