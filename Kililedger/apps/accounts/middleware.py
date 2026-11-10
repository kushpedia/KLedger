"""
Role-Based Access Control (RBAC) Decorators, Mixins, and Tenant Isolation Middleware.
"""
from functools import wraps
from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin


class TenantIsolationMiddleware(MiddlewareMixin):
    """
    Attaches `request.tenant` and `request.active_store_scope` ('all' or specific Store UUID/Kilimall ID)
    on every authenticated vendor request, and synchronizes query param `?store=` with session state.
    """

    def process_request(self, request):
        request.tenant = None
        request.active_store_scope = "all"
        request.active_accounting_basis = "CASH_FLOW"

        user = getattr(request, "user", None)
        if user and user.is_authenticated and getattr(user, "tenant_id", None):
            request.tenant = user.tenant

            # 1. Sync Global Store Context Switcher (?store=all or ?store=10344)
            store_param = request.GET.get("store")
            if store_param:
                request.session["active_store_scope"] = store_param
            request.active_store_scope = request.session.get("active_store_scope", "all")

            # 2. Sync Accounting Mode Toggle (?basis=CASH_FLOW or ?basis=ORDER_ACCRUAL)
            basis_param = request.GET.get("basis")
            if basis_param in ("CASH_FLOW", "ORDER_ACCRUAL"):
                request.session["active_accounting_basis"] = basis_param
            request.active_accounting_basis = request.session.get(
                "active_accounting_basis",
                request.tenant.default_accounting_basis,
            )


def vendor_required(view_func):
    """Ensures the user is logged in and belongs to an active VendorTenant."""

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        if request.user.is_platform_operator and not request.user.tenant_id:
            return redirect("accounts:platform_admin_console")
        if not request.tenant or not request.tenant.is_active:
            messages.error(request, "Your vendor workspace is currently inactive. Please contact support.")
            return redirect("accounts:login")
        return view_func(request, *args, **kwargs)

    return _wrapped_view


def role_required(*allowed_roles):
    """Restricts a view to specific VendorUser roles (e.g., OWNER, FINANCE)."""

    def decorator(view_func):
        @wraps(view_func)
        @vendor_required
        def _wrapped_view(request, *args, **kwargs):
            if request.user.role not in allowed_roles:
                raise PermissionDenied("Your user role does not have permission to perform this action.")
            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator


class VendorRequiredMixin(AccessMixin):
    """Class-based view mixin enforcing active VendorTenant membership and optional role checks."""

    allowed_roles = None  # e.g., ('OWNER', 'FINANCE')

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if request.user.is_platform_operator and not request.user.tenant_id:
            return redirect("accounts:platform_admin_console")
        if not getattr(request, "tenant", None) or not request.tenant.is_active:
            messages.error(request, "Active vendor workspace required.")
            return redirect("accounts:login")
        if self.allowed_roles and request.user.role not in self.allowed_roles:
            raise PermissionDenied("Insufficient role permissions for this financial module.")
        return super().dispatch(request, *args, **kwargs)


class PlatformAdminRequiredMixin(AccessMixin):
    """Restricts access strictly to SaaS Platform Administrators (SCR-16)."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_platform_operator:
            raise PermissionDenied("Platform Administrator privileges required.")
        return super().dispatch(request, *args, **kwargs)
