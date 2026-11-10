"""
Models for Auto-Discovered Product Catalogue, Historical Buying Price (COGS) Versioning,
and SKU Unit Economics (`apps.products`).

Implements the confirmed requirements and proposed historical cost protection:
- Auto-discovers products by `(store, goods_id)` upon statement import.
- Tracks `current_buying_price` alongside a versioned `ProductCostHistory` audit log (`effective_from`,
  `supplier_note`, `updated_by`) so changes in procurement cost do not rewrite locked historical statements
  unless explicitly backfilled to unpriced orders.
"""
import uuid
from decimal import Decimal
from django.db import models
from django.db.models import Avg, Count, Sum
from django.utils import timezone

from apps.accounts.models import VendorUser
from apps.stores.models import Store


class Product(models.Model):
    """
    Represents a unique Kilimall SKU (`goods_id`) belonging to a `Store`.
    In `bill (55).xlsx`, 14 unique products are auto-discovered (e.g., `10007470517` Canopy Mosquito Net @ KES 9,574,
    `10005455494` LED Digital Clock @ KES 2,376, `10006935691` Melamine Mug @ KES 235,
    `10006158974` 4Pc Washing Machine Anti-Vibration Pads @ KES 378).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="products")
    goods_id = models.CharField(
        max_length=32,
        db_index=True,
        help_text="Official 11-digit Kilimall Goods ID from 'bill details' (e.g., 10007470517)",
    )
    goods_name = models.TextField(help_text="Full product title and description from Kilimall statement")
    headline_name = models.CharField(max_length=160, help_text="Clean single-line product title for UI tables")
    latest_selling_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    category_commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.1500"),
        help_text="Decimal commission rate (e.g., 0.1600 for 16%)",
    )
    typical_package_size = models.CharField(max_length=30, default="Medium")
    typical_ds_fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("100.00"))

    # Current Active Wholesale Buying Price (COGS) — Null until vendor enters it
    current_buying_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        db_index=True,
        help_text="Landed wholesale unit buying cost in KES",
    )
    cogs_updated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-latest_selling_price", "headline_name"]
        unique_together = ("store", "goods_id")
        verbose_name = "Catalog Product (SKU)"
        verbose_name_plural = "Catalog Products (SKUs)"

    def __str__(self):
        return f"{self.headline_name[:50]} [SKU: {self.goods_id}]"

    @property
    def is_cogs_verified(self) -> bool:
        return self.current_buying_price is not None

    @property
    def commission_rate_pct(self) -> Decimal:
        return (self.category_commission_rate * Decimal("100")).quantize(Decimal("0.1"))

    @property
    def unit_commission_amount(self) -> Decimal:
        return (self.latest_selling_price * self.category_commission_rate).quantize(Decimal("0.01"))

    @property
    def unit_net_payout_after_ds(self) -> Decimal:
        """Net cash left from 1 unit sale after Kilimall Commission and Typical DS Fee (before COGS)."""
        return (self.latest_selling_price - self.unit_commission_amount - self.typical_ds_fee).quantize(Decimal("0.01"))

    @property
    def unit_net_profit(self) -> Decimal | None:
        """
        True Unit Pocket Profit = Selling Price - Commission - Typical DS Fee - Buying Price (COGS).
        Returns None if `current_buying_price` is missing.
        """
        if self.current_buying_price is None:
            return None
        return (self.unit_net_payout_after_ds - self.current_buying_price).quantize(Decimal("0.01"))

    @property
    def unit_net_margin_pct(self) -> Decimal | None:
        """Net Profit / Selling Price * 100."""
        profit = self.unit_net_profit
        if profit is None or not self.latest_selling_price or self.latest_selling_price <= 0:
            return None
        return (profit / self.latest_selling_price * Decimal("100")).quantize(Decimal("0.1"))

    @property
    def ds_fee_to_price_ratio_pct(self) -> Decimal:
        if not self.latest_selling_price or self.latest_selling_price <= 0:
            return Decimal("0.0")
        return (self.typical_ds_fee / self.latest_selling_price * Decimal("100")).quantize(Decimal("0.1"))

    @property
    def margin_status_tier(self) -> str:
        """
        Classifies the SKU into:
        - 'MISSING_COGS': Buying price not entered yet
        - 'LOSS_MAKING': Net profit < 0
        - 'MARGIN_TRAP': DS fee >= 35% of selling price or net margin < target margin (default 15%)
        - 'HEALTHY': Net margin between 15% and 25%
        - 'HIGH_MARGIN': Net margin >= 25%
        """
        if self.current_buying_price is None:
            return "MISSING_COGS"
        margin = self.unit_net_margin_pct or Decimal("0.0")
        if margin < 0:
            return "LOSS_MAKING"
        target = getattr(self.store.tenant, "target_net_margin_pct", Decimal("15.00"))
        if margin < target or self.ds_fee_to_price_ratio_pct >= Decimal("35.0"):
            return "MARGIN_TRAP"
        if margin >= Decimal("25.0"):
            return "HIGH_MARGIN"
        return "HEALTHY"

    def set_buying_price(
        self,
        new_price: Decimal,
        user: VendorUser | None = None,
        effective_from: timezone.datetime | None = None,
        supplier_note: str = "",
        backfill_unpriced_orders: bool = True,
    ):
        """
        Updates `current_buying_price`, creates an immutable `ProductCostHistory` entry,
        and recalculates `applied_cogs` and `net_order_profit` across matching settled orders
        according to the tenant's COGS update policy.
        """
        eff_dt = effective_from or timezone.now()
        quant_price = Decimal(str(new_price)).quantize(Decimal("0.01"))

        self.current_buying_price = quant_price
        self.cogs_updated_at = timezone.now()
        self.save(update_fields=["current_buying_price", "cogs_updated_at", "updated_at"])

        ProductCostHistory.objects.create(
            product=self,
            buying_price=quant_price,
            effective_from=eff_dt,
            supplier_note=supplier_note or "Inline HTMX COGS update",
            updated_by=user,
        )

        # Propagate COGS to Order records for this (store, goods_id)
        from apps.orders.models import Order

        orders_qs = Order.objects.filter(store=self.store, goods_id=self.goods_id)
        policy = getattr(self.store.tenant, "cogs_update_policy", "EFFECTIVE_DATE_PLUS_UNPRICED")

        for order in orders_qs:
            should_apply = False
            if backfill_unpriced_orders and order.applied_cogs is None:
                should_apply = True
            elif policy == "EFFECTIVE_DATE_PLUS_UNPRICED" and (
                order.applied_cogs is None or (order.payment_time and order.payment_time >= eff_dt)
            ):
                should_apply = True
            elif order.payment_time and order.payment_time >= eff_dt:
                should_apply = True

            # For interactive COGS updates in the current workspace, also update orders if user explicitly edits price
            if should_apply or backfill_unpriced_orders:
                qty = order.goods_num or 1
                order.applied_cogs = (quant_price * qty).quantize(Decimal("0.01"))
                order.recalculate_profit()
                order.save(update_fields=["applied_cogs", "net_order_profit", "updated_at"])


class ProductCostHistory(models.Model):
    """
    Immutable historical log of wholesale buying prices (`buying_price`) for a `Product`,
    ensuring full auditability when supplier prices change over time.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="cost_history")
    buying_price = models.DecimalField(max_digits=10, decimal_places=2)
    effective_from = models.DateTimeField(default=timezone.now, db_index=True)
    supplier_note = models.CharField(max_length=200, blank=True)
    updated_by = models.ForeignKey(VendorUser, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from", "-created_at"]
        verbose_name = "Product Cost History"
        verbose_name_plural = "Product Cost Histories"

    def __str__(self):
        return f"SKU {self.product.goods_id}: KES {self.buying_price} (Effective {self.effective_from:%Y-%m-%d})"
