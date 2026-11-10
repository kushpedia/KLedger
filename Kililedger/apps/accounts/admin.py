from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from apps.accounts.models import ParserSchemaDiagnosticLog, VendorNotification, VendorTenant, VendorUser


@admin.register(VendorTenant)
class VendorTenantAdmin(admin.ModelAdmin):
    list_display = (
        "business_name",
        "contact_email",
        "contact_phone",
        "default_accounting_basis",
        "pending_aging_threshold_days",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "default_accounting_basis")
    search_fields = ("business_name", "contact_email")


@admin.register(VendorUser)
class VendorUserAdmin(UserAdmin):
    list_display = ("username", "email", "tenant", "role", "is_active", "is_staff")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = UserAdmin.fieldsets + (
        ("KiliLedger Multi-Tenant RBAC", {"fields": ("tenant", "role", "phone_number")}),
    )


@admin.register(ParserSchemaDiagnosticLog)
class ParserSchemaDiagnosticLogAdmin(admin.ModelAdmin):
    list_display = ("sheet_name", "issue_type", "kilimall_store_id", "auto_resolved", "created_at")
    list_filter = ("issue_type", "auto_resolved", "sheet_name")
