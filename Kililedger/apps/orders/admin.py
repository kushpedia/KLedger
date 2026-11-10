from django.contrib import admin
from apps.orders.models import LogisticsFee, Order, PenaltyFine


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_sn",
        "store",
        "settlement_status",
        "penalty_status",
        "goods_price",
        "commission_amount",
        "linked_ds_fee",
        "linked_fine_amount",
        "net_order_profit",
        "was_auto_flipped_from_pending",
    )
    list_filter = ("settlement_status", "penalty_status", "has_package_size_anomaly", "store")
    search_fields = ("order_sn", "linked_shipment_no", "goods_id", "headline_name")


@admin.register(LogisticsFee)
class LogisticsFeeAdmin(admin.ModelAdmin):
    list_display = ("shipment_no", "order", "package_size", "amount", "fee_date", "is_size_anomaly")
    list_filter = ("package_size", "is_size_anomaly")
    search_fields = ("shipment_no", "order__order_sn")


@admin.register(PenaltyFine)
class PenaltyFineAdmin(admin.ModelAdmin):
    list_display = ("order", "workbook_store_alias", "reason", "fine_amount", "elapsed_badge", "payment_time", "cancel_time")
    list_filter = ("penalty_code", "workbook_store_alias")
    search_fields = ("order__order_sn", "reason")
