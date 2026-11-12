"""
Views for Authentication (SCR-01), Account Activation & Email Verification,
Vendor Settings & Reconciliation Rules (SCR-15), Slide-Over Notifications Drawer (SCR-15),
and Platform Administration Console (SCR-16).
"""
from django.conf import settings
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
from apps.accounts.tokens import verify_activation_token
from services.email_service import send_vendor_activation_email


class AuthSplitPortalView(View):
    """
    Implements SCR-01: Split-Screen Authentication & Vendor Registration Portal.
    - Vendor Registration creates user with `is_active=False` and dispatches an activation email.
    - Vendor Sign In checks `is_active` and prompts verification if unconfirmed.
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
                # Creates inactive VendorTenant and inactive VendorUser
                user = register_form.save(is_active=False)
                tenant = user.tenant

                # Send activation email with secure 48-hour link
                send_vendor_activation_email(user, request=request)

                is_dev = getattr(settings, "DEBUG", False)
                return render(
                    request,
                    "accounts/activation_sent.html",
                    {
                        "email": user.email,
                        "business_name": tenant.business_name if tenant else "Your Business",
                        "is_development": is_dev,
                    },
                )

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
            if not user.is_active:
                messages.warning(
                    request,
                    "Your account has not been activated yet. Please check your email for the verification link.",
                )
                return render(
                    request,
                    "accounts/activation_sent.html",
                    {
                        "email": user.email,
                        "business_name": user.tenant.business_name if user.tenant else "Your Business",
                        "is_development": getattr(settings, "DEBUG", False),
                    },
                )

            login(request, user)
            if user.is_platform_operator and not user.tenant_id:
                return redirect("accounts:platform_admin_console")
            return redirect("analytics:dashboard")

        return render(
            request,
            self.template_name,
            {"active_tab": "login", "login_form": login_form, "register_form": register_form},
        )


class ActivateAccountView(View):
    """
    Handles `/activate/<token>/` email activation link clicks.
    Validates token cryptographic signature and 48-hour expiration,
    activates `user` and `tenant`, logs in the user, and redirects to onboarding.
    """

    def get(self, request, token):
        user = verify_activation_token(token, max_age_hours=48)
        if user is None:
            return render(request, "accounts/activation_invalid.html")

        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])

        if user.tenant and not user.tenant.is_active:
            user.tenant.is_active = True
            user.tenant.save(update_fields=["is_active"])

        # Auto-authenticate the verified user
        login(request, user)
        messages.success(
            request,
            f"Email verified successfully! Welcome to KiliLedger Pro, {user.tenant.business_name if user.tenant else ''}.",
        )
        return redirect("stores:onboarding_wizard")


class ResendActivationLinkView(View):
    """Allows unverified users to request a fresh activation link if expired or lost."""

    def get(self, request):
        email = request.GET.get("email", "")
        return render(request, "accounts/activation_invalid.html", {"prefill_email": email})

    def post(self, request):
        email = request.POST.get("email", "").strip().lower()
        user = VendorUser.objects.filter(email__iexact=email).first()

        if user:
            if user.is_active:
                messages.info(request, "Your account is already active. Please sign in.")
                return redirect("accounts:login")

            send_vendor_activation_email(user, request=request)
            messages.success(request, f"A fresh activation link has been sent to {email}.")
            return render(
                request,
                "accounts/activation_sent.html",
                {
                    "email": email,
                    "business_name": user.tenant.business_name if user.tenant else "Your Business",
                    "is_development": getattr(settings, "DEBUG", False),
                },
            )

        messages.error(request, "No account was found with that email address.")
        return render(request, "accounts/activation_invalid.html", {"prefill_email": email})


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
