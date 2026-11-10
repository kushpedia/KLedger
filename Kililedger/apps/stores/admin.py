from django.contrib import admin
from apps.stores.models import Store, StoreAlias


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("store_name", "kilimall_store_id", "tenant", "fulfillment_type", "is_active", "created_at")
    list_filter = ("fulfillment_type", "is_active")
    search_fields = ("store_name", "kilimall_store_id", "tenant__business_name")


@admin.register(StoreAlias)
class StoreAliasAdmin(admin.ModelAdmin):
    list_display = ("alias_name", "store", "source_sheet", "discovered_at")
    search_fields = ("alias_name", "store__kilimall_store_id")
