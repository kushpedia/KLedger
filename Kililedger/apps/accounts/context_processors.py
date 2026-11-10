"""
Global Template Context Processor for KiliLedger Pro.
Injects the active store switcher context, accounting mode, and unread notifications
into every template render (including HTMX partial swaps).
"""
from apps.accounts.models import VendorNotification


def global_workspace_context(request):
    """
    Provides global header variables required by `templates/base.html` and `_store_switcher.html`:
    - `current_tenant`: Active VendorTenant instance
    - `vendor_stores`: QuerySet of registered Kilimall stores for this vendor
    - `active_store`: Selected Store object (or None when 'All My Stores' is active)
    - `active_store_scope`: 'all' or specific kilimall_store_id
    - `active_accounting_basis`: 'CASH_FLOW' or 'ORDER_ACCRUAL'
    - `unread_notifications_count`: Count of unread alerts
    - `missing_cogs_count`: Count of auto-discovered products missing buying prices
    """
    if not hasattr(request, "user") or not request.user.is_authenticated or not getattr(request, "tenant", None):
        return {}

    tenant = request.tenant
    vendor_stores = []
    active_store = None
    missing_cogs_count = 0

    # Safely query stores & products once those tables exist
    try:
        from apps.stores.models import Store
        from apps.products.models import Product

        vendor_stores = list(Store.objects.filter(tenant=tenant, is_active=True).prefetch_related("aliases"))
        scope = getattr(request, "active_store_scope", "all")
        if scope and scope != "all":
            active_store = next(
                (s for s in vendor_stores if str(s.kilimall_store_id) == str(scope) or str(s.id) == str(scope)),
                None,
            )

        product_qs = Product.objects.filter(store__tenant=tenant)
        if active_store:
            product_qs = product_qs.filter(store=active_store)
        missing_cogs_count = product_qs.filter(current_buying_price__isnull=True).count()
    except Exception:
        # During initial migrations or before Milestone 2/4 tables are populated
        pass

    unread_notifications = VendorNotification.objects.filter(tenant=tenant, is_read=False)

    return {
        "current_tenant": tenant,
        "vendor_stores": vendor_stores,
        "active_store": active_store,
        "active_store_scope": "all" if active_store is None else active_store.kilimall_store_id,
        "active_accounting_basis": getattr(request, "active_accounting_basis", tenant.default_accounting_basis),
        "unread_notifications_count": unread_notifications.count(),
        "recent_notifications": unread_notifications[:6],
        "missing_cogs_count": missing_cogs_count,
    }
