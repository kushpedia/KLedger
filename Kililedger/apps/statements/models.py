"""
Models for Kilimall Weekly Settlement Statements & Ingestion Audit Trail (`apps.statements`).
"""
import uuid
from decimal import Decimal
from django.db import models
from apps.accounts.models import VendorUser
from apps.stores.models import Store


class SettlementStatement(models.Model):
    """
    Represents a single uploaded Kilimall `.xlsx` settlement statement for one Store and one settlement cycle
    (e.g., `bill (55).xlsx` for `Kushpedia Stores4 [10344]`, covering Oct 26 – Oct 31, 2026).

    Stores the exact Two-Stage Payout Equation figures from Row 2 of the `bill` worksheet AND the
    sanitized row payloads (`completed_sales`, `ds_fees`, `fines`, `discovered_products`, `sheet_checklist`)
    so the digital twin (`SCR-07`) and rollback service have 100% audit fidelity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="statements")
    filename = models.CharField(max_length=255)
    file_checksum = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 hash of the uploaded .xlsx file to prevent duplicate imports",
    )
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField(db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(VendorUser, on_delete=models.SET_NULL, null=True, blank=True)

    # Stage 1: Trading Margin (from 'bill' sheet)
    total_volume = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_commission = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    quality_inspection_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    settlement_payable_1 = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Stage 2: Logistics, Penalties & Final Settlement Payout (from 'bill' sheet)
    fines = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    warehouse_operation_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    warehouse_storage_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    ds_processing_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    lite_shipping_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    other_deductions = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    billing_appeal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    compensations = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    settlement_payable_2 = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    final_settlement_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Row Counts & Reconciliation Summary Metrics
    completed_orders_count = models.PositiveIntegerField(default=0)
    ds_packages_count = models.PositiveIntegerField(default=0)
    fines_count = models.PositiveIntegerField(default=0)
    same_bill_settled_count = models.PositiveIntegerField(default=0)
    prior_pending_auto_settled_count = models.PositiveIntegerField(default=0)
    new_pending_settlement_count = models.PositiveIntegerField(default=0)
    math_reconciled = models.BooleanField(default=True)

    # Full Sanitized Audit Payload (JSONField compatible with both SQLite and PostgreSQL)
    parsed_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-period_end", "-uploaded_at"]
        unique_together = ("store", "file_checksum")
        verbose_name = "Settlement Statement"
        verbose_name_plural = "Settlement Statements"

    def __str__(self):
        return f"{self.filename} — {self.store.store_name} ({self.period_start:%Y-%m-%d} to {self.period_end:%Y-%m-%d})"

    @property
    def total_deductions(self) -> Decimal:
        """Sum of commissions, DS fees, fines, and other fees."""
        return (
            self.total_commission
            + self.quality_inspection_fee
            + self.ds_processing_fee
            + self.fines
            + self.warehouse_operation_fee
            + self.warehouse_storage_fee
            + self.lite_shipping_fee
            + self.other_deductions
        )

    @property
    def effective_commission_pct(self) -> Decimal:
        if self.total_volume and self.total_volume > 0:
            return (abs(self.total_commission) / self.total_volume * Decimal("100")).quantize(Decimal("0.01"))
        return Decimal("0.00")

    @property
    def payout_retention_pct(self) -> Decimal:
        if self.total_volume and self.total_volume > 0:
            return (self.final_settlement_amount / self.total_volume * Decimal("100")).quantize(Decimal("0.01"))
        return Decimal("0.00")

    @property
    def ds_settled_progress_pct(self) -> int:
        if self.ds_packages_count == 0:
            return 100
        return int(round((self.same_bill_settled_count / self.ds_packages_count) * 100))
