"""
Models for Cross-Week Order Reconciliation, Drop-Shipping (DS) Logistics Fees,
and Merchant Penalty Fines (`apps.orders`).

Enforces strict separation of concerns (Section 8.2 of Blueprint):
- `settlement_status`: Tracks whether the order's sales revenue has settled (`PENDING_SETTLEMENT`, `SETTLED`,
  `OVERDUE_PENDING`, `RETURNED_UNSETTLED`, `UNSETTLED_PENALTY_ONLY`).
- `penalty_status`: Tracks whether the order incurred an operational fine (`NONE`, `OVERDUE_DISPATCH`, `OUT_OF_STOCK`).
"""
import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone

from apps.statements.models import SettlementStatement
from apps.stores.models import Store


class Order(models.Model):
    """
    Master Reconciled Order Record keyed by `(store, order_sn)`.
    Links records across multiple weekly Kilimall `.xlsx` uploads:
    - Created as `PENDING_SETTLEMENT` when first seen in `ds processing fee`.
    - Automatically flips to `SETTLED` when matched by `order_sn` in `bill details` (same week or subsequent week).
    - Tracks penalties from the `fine` tab independently of settlement lifecycle.
    """

    STATUS_PENDING = "PENDING_SETTLEMENT"
    STATUS_SETTLED = "SETTLED"
    STATUS_OVERDUE = "OVERDUE_PENDING"
    STATUS_RETURNED = "RETURNED_UNSETTLED"
    STATUS_PENALTY_ONLY = "UNSETTLED_PENALTY_ONLY"

    SETTLEMENT_STATUS_CHOICES = (
        (STATUS_PENDING, "Pending Settlement"),
        (STATUS_SETTLED, "Settled"),
        (STATUS_OVERDUE, "Overdue Pending (>14d)"),
        (STATUS_RETURNED, "Returned to Seller"),
        (STATUS_PENALTY_ONLY, "Unsettled — Penalty Only"),
    )

    PENALTY_NONE = "NONE"
    PENALTY_OVERDUE_DISPATCH = "OVERDUE_DISPATCH"
    PENALTY_OUT_OF_STOCK = "OUT_OF_STOCK"

    PENALTY_STATUS_CHOICES = (
        (PENALTY_NONE, "No Penalty"),
        (PENALTY_OVERDUE_DISPATCH, "Seller Overdue Dispatch Fine (-300 KES)"),
        (PENALTY_OUT_OF_STOCK, "Seller Confirmed Out of Stock (-300 KES)"),
    )

    DS_MATCH_SAME_BILL = "SAME_BILL"
    DS_MATCH_PRIOR_BILL_AUTO_FLIPPED = "AUTO_FLIPPED_FROM_PENDING"
    DS_MATCH_PRIOR_UNTRACKED = "PRIOR_CYCLE_DS"
    DS_MATCH_AWAITING_SALE = "AWAITING_SALE_SETTLEMENT"
    DS_MATCH_NONE = "NO_DS_FEE"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="orders")
    order_sn = models.CharField(
        max_length=32,
        db_index=True,
        help_text="Cleaned 15-digit Kilimall Order SN with leading comma stripped (e.g., 610287144669824)",
    )
    workbook_store_alias = models.CharField(
        max_length=150,
        blank=True,
        help_text="Exact store_name alias seen on the worksheet (e.g., Kushpedia Stores4 or Harambee Ⅰ)",
    )

    # Orthogonal Status Dimensions
    settlement_status = models.CharField(
        max_length=30,
        choices=SETTLEMENT_STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    penalty_status = models.CharField(
        max_length=30,
        choices=PENALTY_STATUS_CHOICES,
        default=PENALTY_NONE,
        db_index=True,
    )
    ds_match_type = models.CharField(max_length=35, default=DS_MATCH_NONE)
    was_auto_flipped_from_pending = models.BooleanField(
        default=False,
        help_text="True when this order started as Pending Settlement in an earlier upload and auto-flipped to Settled",
    )

    # Statement Audit Links
    first_seen_statement = models.ForeignKey(
        SettlementStatement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="first_seen_orders",
    )
    settlement_statement = models.ForeignKey(
        SettlementStatement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="settled_orders",
        help_text="Statement where the completed sale ('bill details') was settled",
    )

    # Product & Completed Sale Fields (Populated when settled in 'bill details')
    goods_id = models.CharField(max_length=32, blank=True, db_index=True)
    goods_name = models.TextField(blank=True)
    headline_name = models.CharField(max_length=160, blank=True)
    goods_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    goods_num = models.PositiveIntegerField(default=1)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_settlement = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Goods Price minus Commission from 'bill details'",
    )
    payment_time = models.DateTimeField(null=True, blank=True, db_index=True)
    finished_time = models.DateTimeField(null=True, blank=True)

    # Aggregated Linked Deductions & Unit Economics
    linked_ds_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    linked_shipment_no = models.CharField(max_length=64, blank=True)
    linked_package_size = models.CharField(max_length=30, blank=True)
    ds_fee_scan_date = models.DateTimeField(null=True, blank=True, db_index=True)

    linked_fine_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    applied_cogs = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    net_order_profit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    has_package_size_anomaly = models.BooleanField(default=False)

    resolution_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-payment_time", "-ds_fee_scan_date", "-created_at"]
        unique_together = ("store", "order_sn")
        verbose_name = "Reconciled Order"
        verbose_name_plural = "Reconciled Orders"

    def __str__(self):
        return f"Order {self.order_sn} [{self.settlement_status}] — {self.store.store_name}"

    @property
    def transit_days_elapsed(self) -> int:
        """Number of days since DS fee hub scan for Pending Settlement orders."""
        anchor = self.ds_fee_scan_date or self.payment_time or self.created_at
        if not anchor:
            return 0
        end_ref = self.finished_time if self.settlement_status == self.STATUS_SETTLED and self.finished_time else timezone.now()
        return max(0, (end_ref - anchor).days)

    @property
    def aging_bucket(self) -> str:
        """Categorizes pending orders into '0_7D', '8_14D', or 'OVERDUE_14D_PLUS'."""
        days = self.transit_days_elapsed
        threshold = getattr(self.store.tenant, "pending_aging_threshold_days", 14)
        if days > threshold:
            return "OVERDUE"
        if days >= 8:
            return "EXTENDED_8_14D"
        return "NORMAL_0_7D"

    @property
    def cash_contribution_after_ds(self) -> Decimal | None:
        """Net Kilimall settlement minus linked DS fee (before COGS)."""
        if self.net_settlement is None:
            return None
        return (self.net_settlement - abs(self.linked_ds_fee)).quantize(Decimal("0.01"))

    def recalculate_profit(self):
        """
        Computes `net_order_profit` when `net_settlement` is present:
        Net Order Profit = Goods Price - Commission - Linked DS Fee - Applied COGS
        """
        if self.net_settlement is not None:
            cash_after_ds = self.net_settlement - abs(self.linked_ds_fee)
            if self.applied_cogs is not None:
                self.net_order_profit = (cash_after_ds - self.applied_cogs).quantize(Decimal("0.01"))
            else:
                self.net_order_profit = None


class LogisticsFee(models.Model):
    """
    Granular Drop-Shipping (DS) Processing Fee record from the `ds processing fee` worksheet.
    Package tiers: Super Small (50), Small (80), Medium (100), Large (200), Super Large (250).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="logistics_fees")
    statement = models.ForeignKey(SettlementStatement, on_delete=models.CASCADE, related_name="logistics_fees")
    shipment_no = models.CharField(max_length=64, db_index=True)
    package_size = models.CharField(max_length=30, db_index=True)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    fee_date = models.DateTimeField(db_index=True)
    is_size_anomaly = models.BooleanField(default=False)

    class Meta:
        ordering = ["-fee_date"]
        unique_together = ("statement", "order", "shipment_no")

    def __str__(self):
        return f"{self.shipment_no} ({self.package_size}: KES {self.amount}) — Order {self.order.order_sn}"


class PenaltyFine(models.Model):
    """
    Operational SLA penalty record from the `fine` worksheet (`KES 300.00` per incident:
    'Seller overdue dispatch' or 'Seller confirmed out of stock').
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="penalties")
    statement = models.ForeignKey(SettlementStatement, on_delete=models.CASCADE, related_name="penalties")
    workbook_store_alias = models.CharField(max_length=150, default="Harambee Ⅰ")
    penalty_code = models.CharField(max_length=30, default=Order.PENALTY_OVERDUE_DISPATCH)
    reason = models.CharField(max_length=150)
    fine_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("300.00"))
    payment_time = models.DateTimeField(null=True, blank=True)
    cancel_time = models.DateTimeField(null=True, blank=True)
    elapsed_delay_seconds = models.PositiveIntegerField(default=0)
    elapsed_badge = models.CharField(max_length=32, default="—")
    remark = models.CharField(max_length=32, default="GS")

    class Meta:
        ordering = ["-payment_time"]

    def __str__(self):
        return f"Fine KES {self.fine_amount} ({self.reason}) — Order {self.order.order_sn}"
