from django.contrib import admin
from apps.statements.models import SettlementStatement


@admin.register(SettlementStatement)
class SettlementStatementAdmin(admin.ModelAdmin):
    list_display = (
        "filename",
        "store",
        "period_start",
        "period_end",
        "total_volume",
        "final_settlement_amount",
        "completed_orders_count",
        "ds_packages_count",
        "fines_count",
        "math_reconciled",
    )
    list_filter = ("math_reconciled", "store")
    search_fields = ("filename", "store__store_name", "store__kilimall_store_id")
