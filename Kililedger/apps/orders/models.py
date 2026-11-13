"""
Models for Cross-Week Order Reconciliation, Drop-Shipping (DS) Logistics Fees,
Merchant Penalty Fines, Other Deductions (`violation` & `After-sale return shipping`),
and Customer Return Condition Tracking (`apps.orders`).
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
    - Automatically flips to `SETTLED` when matched by `order_sn` in `bill details` with `goods_num > 0`.
    - Automatically transitions to `RETURNED_UNSETTLED` when matched by `order_sn` in `bill details` with `goods_num < 0`
      or when an `After-sale return shipping` deduction is recorded in `Other Deductions`.
    - Tracks penalties from the `fine` tab and post-sale charges from `Other Deductions`.
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
        (STATUS_RETURNED, "Returned / Clawback"),
        (STATUS_PENALTY_ONLY, "Unsettled — Penalty / Deduction Only"),
    )

    PENALTY_NONE = "NONE"
    PENALTY_OVERDUE_DISPATCH = "OVERDUE_DISPATCH"
    PENALTY_OUT_OF_STOCK = "OUT_OF_STOCK"
    PENALTY_DS_LATE_ARRIVAL = "DS_LATE_ARRIVAL"
    PENALTY_POST_SALE_VIOLATION = "POST_SALE_VIOLATION"

    PENALTY_STATUS_CHOICES = (
        (PENALTY_NONE, "No Penalty"),
        (PENALTY_OVERDUE_DISPATCH, "Seller Overdue Dispatch Fine (-300 KES)"),
        (PENALTY_OUT_OF_STOCK, "Seller Confirmed Out of Stock (-300 KES)"),
        (PENALTY_DS_LATE_ARRIVAL, "DS Late Arrival Fine (-200 KES)"),
        (PENALTY_POST_SALE_VIOLATION, "Other Deduction: Violation / Return Shipping"),
    )

    RETURN_COND_NA = "NOT_APPLICABLE"
    RETURN_COND_RESTOCKED = "RESTOCKED"
    RETURN_COND_DAMAGED = "DAMAGED_LOST"

    RETURN_CONDITION_CHOICES = (
        (RETURN_COND_NA, "Not Applicable (Normal Order)"),
        (RETURN_COND_RESTOCKED, "Resellable / Restocked (COGS Recovered)"),
        (RETURN_COND_DAMAGED, "Damaged / Lost (COGS Written Off)"),
    )

    DS_MATCH_SAME_BILL = "SAME_BILL"
    DS_MATCH_PRIOR_BILL_AUTO_FLIPPED = "AUTO_FLIPPED_FROM_PENDING"
    DS_MATCH_PRIOR_UNTRACKED = "PRIOR_CYCLE_DS"
    DS_MATCH_AWAITING_SALE = "AWAITING_SALE_SETTLEMENT"
    DS_MATCH_RETURN_CLAWBACK = "RETURN_CLAWBACK"
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
    return_condition = models.CharField(
        max_length=25,
        choices=RETURN_CONDITION_CHOICES,
        default=RETURN_COND_NA,
        db_index=True,
        help_text="Tracks whether a returned unit (`goods_num < 0`) is Resellable (COGS recovered) or Damaged/Lost (COGS lost)",
    )
    is_return_clawback = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when `bill details` recorded a negative `goods_num` (e.g. -1) clawing back a prior payout",
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
        help_text="Statement where the completed sale or return clawback ('bill details') was recorded",
    )

    # Product & Completed Sale / Return Clawback Fields (Populated from 'bill details')
    goods_id = models.CharField(max_length=32, blank=True, db_index=True)
    goods_name = models.TextField(blank=True)
    headline_name = models.CharField(max_length=160, blank=True)
    goods_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Unit catalog selling price from 'bill details'",
    )
    goods_num = models.IntegerField(
        default=1,
        help_text="Signed quantity from 'bill details': positive (1, 2) for sales, negative (-1) for customer returns",
    )
    complete_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Signed line gross total (`goods_price * goods_num`, e.g. +1340.00 for 2x670 or -999.00 for -1x999)",
    )
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    commission_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Negative on normal sales (e.g. -293.28); positive on returns when Kilimall refunds commission (+149.85)",
    )
    net_settlement = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Positive on normal sales (+1539.72); negative on return clawbacks (-849.15)",
    )
    payment_time = models.DateTimeField(null=True, blank=True, db_index=True)
    finished_time = models.DateTimeField(null=True, blank=True)

    # Aggregated Linked Deductions & Unit Economics
    linked_ds_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    linked_shipment_no = models.CharField(max_length=64, blank=True)
    linked_package_size = models.CharField(max_length=30, blank=True)
    ds_fee_scan_date = models.DateTimeField(null=True, blank=True, db_index=True)

    linked_fine_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    linked_other_deduction = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Sum of deductions from the 'Other Deductions' sheet (e.g., 'violation' 500 + 'After-sale return shipping' 200 = 700.00)",
    )
    unit_buying_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Wholesale unit buying cost (per 1 unit) snapshot from Product Catalogue",
    )
    applied_cogs = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
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
    def effective_gross_amount(self) -> Decimal | None:
        if self.complete_amount is not None:
            return self.complete_amount
        if self.goods_price is not None:
            return (self.goods_price * Decimal(self.goods_num)).quantize(Decimal("0.01"))
        return None

    @property
    def transit_days_elapsed(self) -> int:
        anchor = self.ds_fee_scan_date or self.payment_time or self.created_at
        if not anchor:
            return 0
        end_ref = self.finished_time if self.settlement_status == self.STATUS_SETTLED and self.finished_time else timezone.now()
        return max(0, (end_ref - anchor).days)

    @property
    def aging_bucket(self) -> str:
        days = self.transit_days_elapsed
        threshold = getattr(self.store.tenant, "pending_aging_threshold_days", 14)
        if days > threshold:
            return "OVERDUE"
        if days >= 8:
            return "EXTENDED_8_14D"
        return "NORMAL_0_7D"

    @property
    def cash_contribution_after_ds(self) -> Decimal | None:
        other_ded = abs(getattr(self, "linked_other_deduction", Decimal("0.00")) or Decimal("0.00"))
        if self.settlement_status == self.STATUS_RETURNED or (self.goods_num is not None and self.goods_num < 0):
            return (-abs(self.linked_ds_fee) - other_ded).quantize(Decimal("0.01"))
        if self.net_settlement is None:
            if other_ded > 0:
                return (-abs(self.linked_ds_fee) - other_ded).quantize(Decimal("0.01"))
            return None
        return (self.net_settlement - abs(self.linked_ds_fee) - other_ded).quantize(Decimal("0.01"))

    def recalculate_profit(self):
        """
        Computes `applied_cogs` and `net_order_profit` factoring in:
        - Normal Sales (`goods_num > 0`) vs. Customer Returns (`goods_num < 0` / `STATUS_RETURNED`)
        - `return_condition` (`RESTOCKED` vs. `DAMAGED_LOST`)
        - `linked_ds_fee` and `linked_other_deduction` (`violation` + `After-sale return shipping`).
        """
        qty_abs = abs(int(self.goods_num or 1)) or 1
        other_ded = abs(getattr(self, "linked_other_deduction", Decimal("0.00")) or Decimal("0.00"))

        if self.settlement_status == self.STATUS_RETURNED or (self.goods_num is not None and self.goods_num < 0):
            if self.return_condition == self.RETURN_COND_NA:
                self.return_condition = self.RETURN_COND_RESTOCKED

            if self.return_condition == self.RETURN_COND_DAMAGED:
                if self.unit_buying_price is not None:
                    self.applied_cogs = (self.unit_buying_price * Decimal(qty_abs)).quantize(Decimal("0.01"))
                    self.net_order_profit = (-abs(self.linked_ds_fee) - other_ded - self.applied_cogs).quantize(Decimal("0.01"))
                elif self.applied_cogs is not None and self.applied_cogs > 0:
                    self.net_order_profit = (-abs(self.linked_ds_fee) - other_ded - self.applied_cogs).quantize(Decimal("0.01"))
                else:
                    self.applied_cogs = None
                    self.net_order_profit = (-abs(self.linked_ds_fee) - other_ded).quantize(Decimal("0.01"))
            else:
                self.applied_cogs = Decimal("0.00")
                self.net_order_profit = (-abs(self.linked_ds_fee) - other_ded).quantize(Decimal("0.01"))
            return

        if self.unit_buying_price is not None:
            self.applied_cogs = (self.unit_buying_price * Decimal(qty_abs)).quantize(Decimal("0.01"))

        if self.net_settlement is not None:
            cash_after_ds = self.net_settlement - abs(self.linked_ds_fee) - other_ded
            if self.applied_cogs is not None:
                self.net_order_profit = (cash_after_ds - self.applied_cogs).quantize(Decimal("0.01"))
            else:
                self.net_order_profit = None
        elif other_ded > 0:
            self.net_order_profit = (-abs(self.linked_ds_fee) - other_ded).quantize(Decimal("0.01"))


class LogisticsFee(models.Model):
    """
    Granular Drop-Shipping (DS) Processing Fee record from the `ds processing fee` worksheet.
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
    Operational SLA penalty record from the `fine` worksheet (`KES 300.00` or `KES 200.00` per incident:
    'Seller overdue dispatch', 'Seller confirmed out of stock', or 'DS late arrival').
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


class OtherDeduction(models.Model):
    """
    Granular record from the `Other Deductions` worksheet (Sheet 7 of Kilimall `.xlsx` statement).
    Tracks post-sale deductions such as:
    - `violation` (e.g. `-KES 500.00` after-sale dispute / quality violation fine)
    - `After-sale return shipping` (e.g. `-KES 200.00` reverse courier fee charged on a returned order)
    """

    CODE_VIOLATION = "VIOLATION"
    CODE_RETURN_SHIPPING = "AFTER_SALE_RETURN_SHIPPING"
    CODE_OTHER = "OTHER_DEDUCTION"

    DEDUCTION_CODE_CHOICES = (
        (CODE_VIOLATION, "Post-Sale Violation Penalty"),
        (CODE_RETURN_SHIPPING, "After-Sale Return Shipping Fee"),
        (CODE_OTHER, "Other Platform Deduction"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="other_deductions")
    statement = models.ForeignKey(SettlementStatement, on_delete=models.CASCADE, related_name="other_deductions_rows")
    workbook_store_alias = models.CharField(max_length=150, blank=True)
    sku = models.CharField(max_length=32, blank=True, db_index=True)
    deduction_type = models.CharField(max_length=100, db_index=True)
    deduction_code = models.CharField(max_length=35, choices=DEDUCTION_CODE_CHOICES, default=CODE_OTHER, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    period = models.CharField(max_length=64, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Other Deduction"
        verbose_name_plural = "Other Deductions"

    def __str__(self):
        return f"{self.deduction_type} (-KES {self.amount}) — Order {self.order.order_sn} (SKU {self.sku})"
