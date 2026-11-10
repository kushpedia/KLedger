from django.contrib import admin
from apps.products.models import Product, ProductCostHistory


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "goods_id",
        "headline_name",
        "store",
        "latest_selling_price",
        "category_commission_rate",
        "typical_ds_fee",
        "current_buying_price",
        "cogs_updated_at",
    )
    list_filter = ("store", "category_commission_rate")
    search_fields = ("goods_id", "headline_name", "goods_name")


@admin.register(ProductCostHistory)
class ProductCostHistoryAdmin(admin.ModelAdmin):
    list_display = ("product", "buying_price", "effective_from", "supplier_note", "updated_by", "created_at")
    search_fields = ("product__goods_id", "product__headline_name", "supplier_note")
