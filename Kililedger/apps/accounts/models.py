"""
Multi-Tenant Vendor Account, RBAC User, and Notification Models for KiliLedger Pro.
"""
import uuid
from decimal import Decimal
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class TenantQuerySet(models.QuerySet):
    """
    Base QuerySet enforcing strict tenant data isolation across all vendor queries.
    """

    def for_tenant(self, tenant):
        if tenant is None:
            return self.none()
        return self.filter(tenant=tenant)

    def for_user(self, user):
        if not user or not user.is_authenticated or getattr(user, "tenant_id", None) is None:
            return self.none()
        return self.filter(tenant_id=user.tenant_id)


class TenantManager(models.Manager):
    """Manager wrapping TenantQuerySet for multi-vendor isolation."""

    def get_queryset(self):
        return TenantQuerySet(self.model, using=self._db)

    def for_tenant(self, tenant):
        return self.get_queryset().for_tenant(tenant)

    def for_user(self, user):
        return self.get_queryset().for_user(user)


class VendorTenant(models.Model):
    """
    Represents an isolated Kilimall Seller Business Account (Tenant).
    One VendorTenant can own multiple Kilimall Stores (e.g., Kushpedia Stores4 [10344])
    and invite multiple staff members (Finance, Operations).
    """

    ACCOUNTING_BASIS_CHOICES = (
        ("CASH_FLOW", "Settlement Cash-Flow Basis (Default)"),
        ("ORDER_ACCRUAL", "Order-Matched Accrual Basis"),
    )

    COGS_UPDATE_POLICY_CHOICES = (
        ("EFFECTIVE_DATE_PLUS_UNPRICED", "Apply from effective date forward + backfill unpriced orders"),
        ("EFFECTIVE_DATE_ONLY", "Apply strictly from effective date forward"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business_name = models.CharField(max_length=150, help_text="Registered business or seller organization name")
    contact_email = models.EmailField(unique=True)
    contact_phone = models.CharField(max_length=25, help_text="Kenya phone format (+254...)")
    is_active = models.BooleanField(default=True)

    # Approved Financial & Reconciliation Rules (Confirmed Defaults)
    default_accounting_basis = models.CharField(
        max_length=20,
        choices=ACCOUNTING_BASIS_CHOICES,
        default="CASH_FLOW",
        help_text="Default accounting mode on Executive Dashboard (SCR-03)",
    )
    pending_aging_threshold_days = models.PositiveIntegerField(
        default=14,
        help_text="Days after DS fee scan before a Pending Settlement order is flagged Overdue Pending",
    )
    critical_aging_threshold_days = models.PositiveIntegerField(
        default=21,
        help_text="Days after DS fee scan to escalate for courier return or lost parcel claim",
    )
    target_net_margin_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("15.00"),
        help_text="Minimum target net profit margin percentage before flagging Thin Margin SKUs",
    )
    flag_package_size_anomalies = models.BooleanField(
        default=True,
        help_text="Automatically alert when DS processing fee exceeds 35% of product selling price",
    )
    cogs_update_policy = models.CharField(
        max_length=35,
        choices=COGS_UPDATE_POLICY_CHOICES,
        default="EFFECTIVE_DATE_PLUS_UNPRICED",
    )

    # Privacy-First Support Access Token (Blocks Platform Admin from viewing financial numbers by default)
    support_access_granted_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When set in the future, allows Platform Support read-only troubleshooting access",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Vendor Tenant"
        verbose_name_plural = "Vendor Tenants"

    def __str__(self):
        return f"{self.business_name} ({self.contact_email})"

    @property
    def is_support_access_active(self) -> bool:
        return bool(
            self.support_access_granted_until
            and self.support_access_granted_until > timezone.now()
        )

    def grant_temporary_support_access(self, hours: int = 24):
        self.support_access_granted_until = timezone.now() + timezone.timedelta(hours=hours)
        self.save(update_fields=["support_access_granted_until", "updated_at"])

    def revoke_support_access(self):
        self.support_access_granted_until = None
        self.save(update_fields=["support_access_granted_until", "updated_at"])


class TenantScopedModel(models.Model):
    """
    Abstract base model for all tenant-owned records requiring strict isolation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        VendorTenant,
        on_delete=models.CASCADE,
        related_name="%(class)s_records",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        abstract = True


class VendorUser(AbstractUser):
    """
    Custom user model supporting Vendor Owners, Finance Managers, Operations Assistants,
    and SaaS Platform Administrators.
    """

    ROLE_OWNER = "OWNER"
    ROLE_FINANCE = "FINANCE"
    ROLE_OPERATIONS = "OPERATIONS"
    ROLE_PLATFORM_ADMIN = "PLATFORM_ADMIN"

    ROLE_CHOICES = (
        (ROLE_OWNER, "Vendor Owner (Full Access)"),
        (ROLE_FINANCE, "Finance Manager (Statements, COGS & Reports)"),
        (ROLE_OPERATIONS, "Operations Assistant (Pending Orders & Penalties Only)"),
        (ROLE_PLATFORM_ADMIN, "Platform Administrator (SaaS Ops & Parser Health)"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        VendorTenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="users",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_OWNER)
    phone_number = models.CharField(max_length=25, blank=True)

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self):
        tenant_label = self.tenant.business_name if self.tenant else "Platform Ops"
        return f"{self.get_full_name() or self.username} [{self.role} - {tenant_label}]"

    @property
    def can_view_financials(self) -> bool:
        """Operations Assistants are restricted from viewing net profit and COGS."""
        return self.role in (self.ROLE_OWNER, self.ROLE_FINANCE)

    @property
    def can_manage_stores_and_rollback(self) -> bool:
        """Only the Vendor Owner can delete stores, rollback statements, or grant support access."""
        return self.role == self.ROLE_OWNER

    @property
    def can_upload_statements_and_edit_cogs(self) -> bool:
        return self.role in (self.ROLE_OWNER, self.ROLE_FINANCE)

    @property
    def is_platform_operator(self) -> bool:
        return self.is_staff or self.is_superuser or self.role == self.ROLE_PLATFORM_ADMIN


class VendorNotification(TenantScopedModel):
    """
    Actionable alerts surfaced in the sticky top header notification bell & slide-over drawer (SCR-15).
    """

    CATEGORY_RECONCILIATION = "RECONCILIATION"
    CATEGORY_COGS = "COGS"
    CATEGORY_PENALTY = "PENALTY"
    CATEGORY_ANOMALY = "ANOMALY"

    CATEGORY_CHOICES = (
        (CATEGORY_RECONCILIATION, "Order Reconciliation & Auto-Settlement"),
        (CATEGORY_COGS, "Missing Product Buying Price (COGS)"),
        (CATEGORY_PENALTY, "Fines & Overdue Dispatch Penalties"),
        (CATEGORY_ANOMALY, "DS Package Size Fee Anomaly"),
    )

    SEVERITY_SUCCESS = "SUCCESS"
    SEVERITY_WARNING = "WARNING"
    SEVERITY_DANGER = "DANGER"
    SEVERITY_INFO = "INFO"

    SEVERITY_CHOICES = (
        (SEVERITY_SUCCESS, "Success (Green)"),
        (SEVERITY_WARNING, "Warning (Amber)"),
        (SEVERITY_DANGER, "Critical / Danger (Red)"),
        (SEVERITY_INFO, "Information (Blue)"),
    )

    category = models.CharField(max_length=25, choices=CATEGORY_CHOICES, db_index=True)
    severity = models.CharField(max_length=15, choices=SEVERITY_CHOICES, default=SEVERITY_INFO)
    title = models.CharField(max_length=180)
    message = models.TextField()
    action_url = models.CharField(max_length=255, blank=True)
    action_label = models.CharField(max_length=80, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["is_read", "-created_at"]


class ParserSchemaDiagnosticLog(models.Model):
    """
    Anonymized structural diagnostic log for Platform Administrators (SCR-16)
    to inspect workbook column variations (e.g., 'amout' vs 'amount') without exposing vendor financials.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(VendorTenant, on_delete=models.SET_NULL, null=True, blank=True)
    kilimall_store_id = models.CharField(max_length=32, blank=True)
    sheet_name = models.CharField(max_length=64)
    issue_type = models.CharField(
        max_length=40,
        help_text="HEADER_ALIAS_MAPPED | MISSING_SHEET | DUPLICATE_BLOCKED | MATH_MISMATCH",
    )
    diagnostic_detail = models.TextField(
        help_text="Structural detail only — never logs private currency figures"
    )
    auto_resolved = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
