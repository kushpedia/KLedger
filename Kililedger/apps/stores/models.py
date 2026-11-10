"""
Models for Kilimall Store Management & Workbook Alias Tracking (`apps.stores`).
"""
import uuid
from decimal import Decimal
from django.db import models
from apps.accounts.models import TenantManager, VendorTenant


class Store(models.Model):
    """
    Represents a Kilimall Seller Store owned by a VendorTenant (e.g., Kushpedia Stores4 [10344]).
    Keyed by `kilimall_store_id` so all statements, orders, DS fees, and fines link reliably
    even when secondary aliases (such as 'Harambee Ⅰ' on the 'fine' sheet) appear in Excel files.
    """

    FULFILLMENT_DS = "DS"
    FULFILLMENT_FBK = "FBK"

    FULFILLMENT_CHOICES = (
        (FULFILLMENT_DS, "Drop Shipping (DS — Standard Package Fee Tiers)"),
        (FULFILLMENT_FBK, "Fulfilled by Kilimall (FBK — Warehouse Storage & Op Fees)"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(VendorTenant, on_delete=models.CASCADE, related_name="stores")
    kilimall_store_id = models.CharField(
        max_length=32,
        db_index=True,
        help_text="Official numeric Kilimall Store ID from Column A of the 'bill' worksheet (e.g., 10344)",
    )
    store_name = models.CharField(
        max_length=150,
        help_text="Primary display name for this Kilimall store (e.g., Kushpedia Stores4)",
    )
    fulfillment_type = models.CharField(
        max_length=10,
        choices=FULFILLMENT_CHOICES,
        default=FULFILLMENT_DS,
    )
    currency = models.CharField(max_length=10, default="KES")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        ordering = ["store_name"]
        unique_together = ("tenant", "kilimall_store_id")
        verbose_name = "Kilimall Store"
        verbose_name_plural = "Kilimall Stores"

    def __str__(self):
        return f"{self.store_name} [ID: {self.kilimall_store_id}]"

    def register_alias(self, alias_name: str, source_sheet: str = "bill"):
        """Records a discovered workbook store_name alias (e.g., 'Harambee Ⅰ' from 'fine' tab)."""
        cleaned = (alias_name or "").strip()
        if cleaned:
            StoreAlias.objects.get_or_create(
                store=self,
                alias_name=cleaned,
                defaults={"source_sheet": source_sheet},
            )

    @property
    def secondary_aliases(self):
        return [a.alias_name for a in self.aliases.all() if a.alias_name != self.store_name]

    @property
    def total_statements_count(self) -> int:
        return self.statements.count()

    @property
    def lifetime_gross_sales(self) -> Decimal:
        val = self.statements.aggregate(total=models.Sum("total_volume"))["total"]
        return val or Decimal("0.00")

    @property
    def lifetime_net_payout(self) -> Decimal:
        val = self.statements.aggregate(total=models.Sum("final_settlement_amount"))["total"]
        return val or Decimal("0.00")

    @property
    def lifetime_fines(self) -> Decimal:
        val = self.statements.aggregate(total=models.Sum("fines"))["total"]
        return val or Decimal("0.00")


class StoreAlias(models.Model):
    """
    Stores all `store_name` variations observed across the 9 worksheets for a given `Store ID`
    (for example, `Kushpedia Stores4` on `bill` and `Harambee Ⅰ` on `fine` for Store ID `10344`).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="aliases")
    alias_name = models.CharField(max_length=150)
    source_sheet = models.CharField(max_length=50, default="bill")
    discovered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["discovered_at"]
        unique_together = ("store", "alias_name")
        verbose_name = "Store Workbook Alias"
        verbose_name_plural = "Store Workbook Aliases"

    def __str__(self):
        return f"{self.alias_name} ({self.source_sheet}) -> Store {self.store.kilimall_store_id}"
