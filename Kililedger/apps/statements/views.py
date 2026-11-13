"""
Views for Interactive Excel Upload & 9-Sheet Validation Preview (SCR-05),
Settlement Statements Ledger & Rollback (SCR-06), and Single Statement Detail (SCR-07).
"""
import datetime
from decimal import Decimal
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from apps.accounts.middleware import VendorRequiredMixin
from apps.accounts.models import ParserSchemaDiagnosticLog, VendorNotification, VendorUser
from apps.statements.forms import StatementExcelUploadForm
from apps.statements.models import SettlementStatement
from apps.stores.models import Store
from services.parser import KilimallWorkbookParser


def _parse_iso_dt(dt_str: str | None) -> datetime.datetime:
    if not dt_str:
        return timezone.now()
    try:
        naive = datetime.datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
        return timezone.make_aware(naive) if timezone.is_naive(naive) else naive
    except ValueError:
        return timezone.now()


class StatementUploadWizardView(VendorRequiredMixin, View):
    """
    Implements SCR-05: 3-Stage Interactive Excel Statement Upload, 9-Sheet Structural Validation,
    Duplicate Gate, and Pre-Commit Reconciliation Impact Preview.
    """

    template_name = "statements/upload_preview.html"
    allowed_roles = (VendorUser.ROLE_OWNER, VendorUser.ROLE_FINANCE)

    def get(self, request):
        form = StatementExcelUploadForm(tenant=request.tenant)
        preview_data = request.session.get("pending_statement_preview")
        duplicate_statement = None
        matched_store = None

        if preview_data and preview_data.get("kilimall_store_id"):
            matched_store = Store.objects.filter(
                tenant=request.tenant,
                kilimall_store_id=preview_data["kilimall_store_id"],
            ).first()
            if matched_store:
                duplicate_statement = SettlementStatement.objects.filter(
                    store=matched_store,
                    file_checksum=preview_data["file_checksum"],
                ).first()

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "preview": preview_data,
                "matched_store": matched_store,
                "duplicate_statement": duplicate_statement,
                "active_preview_tab": request.GET.get("tab", "sales"),
            },
        )

    def post(self, request):
        stage = request.POST.get("upload_stage", "validate_file")

        # Clear / Discard Current Preview
        if stage == "discard_preview":
            request.session.pop("pending_statement_preview", None)
            messages.info(request, "Upload preview discarded. You can select another Excel statement.")
            return redirect("statements:upload_wizard")

        # STAGE 2 -> STAGE 3: Confirm & Commit Validated Statement to Database
        if stage == "commit_import":
            preview_data = request.session.get("pending_statement_preview")
            if not preview_data or not preview_data.get("is_valid"):
                messages.error(request, "No valid statement preview found in session. Please upload your file again.")
                return redirect("statements:upload_wizard")

            store_id_str = preview_data["kilimall_store_id"]
            # Check that no other vendor owns this Store ID
            other_owner = Store.objects.filter(kilimall_store_id=store_id_str).exclude(tenant=request.tenant).exists()
            if other_owner:
                messages.error(
                    request,
                    f"Store ID {store_id_str} belongs to another vendor tenant and cannot be imported here.",
                )
                return redirect("statements:upload_wizard")

            with transaction.atomic():
                # 1. Auto-create or fetch Store for this tenant
                store, _ = Store.objects.get_or_create(
                    tenant=request.tenant,
                    kilimall_store_id=store_id_str,
                    defaults={
                        "store_name": preview_data.get("primary_store_name") or f"Kilimall Store {store_id_str}",
                        "fulfillment_type": Store.FULFILLMENT_DS,
                    },
                )
                # Register all discovered workbook aliases (e.g., Kushpedia Stores4 + Harambee Ⅰ)
                for alias_item in preview_data.get("discovered_aliases", []):
                    store.register_alias(alias_item["alias_name"], alias_item["source_sheet"])

                # 2. Prevent duplicate import
                existing_stmt = SettlementStatement.objects.filter(
                    store=store,
                    file_checksum=preview_data["file_checksum"],
                ).first()
                if existing_stmt:
                    request.session.pop("pending_statement_preview", None)
                    messages.warning(
                        request,
                        f"Statement '{existing_stmt.filename}' was already imported for {store.store_name}. Redirected to existing record.",
                    )
                    return redirect("statements:statement_detail", pk=existing_stmt.pk)

                # 3. Create SettlementStatement record
                summary = preview_data["summary"]
                impact = preview_data["reconciliation_impact"]
                statement = SettlementStatement.objects.create(
                    store=store,
                    filename=preview_data["filename"],
                    file_checksum=preview_data["file_checksum"],
                    period_start=_parse_iso_dt(preview_data.get("period_start")),
                    period_end=_parse_iso_dt(preview_data.get("period_end")),
                    uploaded_by=request.user,
                    total_volume=Decimal(summary["total_volume"]),
                    total_commission=Decimal(summary["total_commission"]),
                    quality_inspection_fee=Decimal(summary["quality_inspection_fee"]),
                    settlement_payable_1=Decimal(summary["settlement_payable_1"]),
                    fines=Decimal(summary["fines"]),
                    warehouse_operation_fee=Decimal(summary["warehouse_operation_fee"]),
                    warehouse_storage_fee=Decimal(summary["warehouse_storage_fee"]),
                    ds_processing_fee=Decimal(summary["ds_processing_fee"]),
                    lite_shipping_fee=Decimal(summary["lite_shipping_fee"]),
                    other_deductions=Decimal(summary["other_deductions"]),
                    billing_appeal=Decimal(summary["billing_appeal"]),
                    compensations=Decimal(summary["compensations"]),
                    settlement_payable_2=Decimal(summary["settlement_payable_2"]),
                    final_settlement_amount=Decimal(summary["final_settlement_amount"]),
                    completed_orders_count=impact["completed_sales_count"],
                    ds_packages_count=impact["ds_fees_count"],
                    fines_count=impact["fines_count"],
                    same_bill_settled_count=impact["same_bill_settled_count"],
                    prior_pending_auto_settled_count=impact["prior_pending_auto_settled_count"],
                    new_pending_settlement_count=impact["new_pending_settlement_count"],
                    math_reconciled=preview_data.get("math_reconciled", True),
                    parsed_payload=preview_data,
                )

                # 4. Log anonymized schema diagnostics for Platform Admin (SCR-16)
                for diag in preview_data.get("schema_diagnostics", []):
                    ParserSchemaDiagnosticLog.objects.create(
                        tenant=request.tenant,
                        kilimall_store_id=store_id_str,
                        sheet_name=diag["sheet_name"],
                        issue_type=diag["issue_type"],
                        diagnostic_detail=diag["diagnostic_detail"],
                        auto_resolved=diag.get("auto_resolved", True),
                    )

                # 5. Invoke Cross-Week Order & Product Reconciliation Hook
                from services.reconciler import commit_statement_orders_and_products
                commit_statement_orders_and_products(statement=statement, preview_data=preview_data, user=request.user)

                # 6. Create Actionable Vendor Notifications (SCR-15 Drawer)
                VendorNotification.objects.create(
                    tenant=request.tenant,
                    category=VendorNotification.CATEGORY_RECONCILIATION,
                    severity=VendorNotification.SEVERITY_SUCCESS,
                    title=f"Statement '{statement.filename}' Imported (KES {statement.final_settlement_amount})",
                    message=(
                        f"Reconciled {statement.completed_orders_count} completed sales and {statement.ds_packages_count} DS packages "
                        f"for {store.store_name}. {statement.new_pending_settlement_count} DS packages are now tracked in Pending Settlement."
                    ),
                    action_url=f"/statements/{statement.id}/",
                    action_label="Open Statement Audit",
                )

                if impact.get("package_anomalies_count", 0) > 0:
                    anom = impact["package_anomalies"][0]
                    VendorNotification.objects.create(
                        tenant=request.tenant,
                        category=VendorNotification.CATEGORY_ANOMALY,
                        severity=VendorNotification.SEVERITY_DANGER,
                        title=f"DS Package Size Overcharge Detected on Order {anom['order_sn']}",
                        message=(
                            f"{anom['headline_name']} (Price KES {anom['goods_price']}) was billed as '{anom['package_size']}' "
                            f"(-KES {anom['ds_fee']}, {anom['ratio_pct']}% of item price) on shipment {anom['shipment_no']}."
                        ),
                        action_url=f"/statements/{statement.id}/?tab=ds_fees",
                        action_label="Inspect DS Fee Anomaly",
                    )

            request.session.pop("pending_statement_preview", None)
            request.session["active_store_scope"] = store.kilimall_store_id
            messages.success(
                request,
                f"Statement '{statement.filename}' committed! Final Payout: KES {statement.final_settlement_amount:,.2f}.",
            )
            return redirect("statements:statement_detail", pk=statement.pk)

        # STAGE 1: Upload & Validate .xlsx File -> Store Preview in Session
        form = StatementExcelUploadForm(tenant=request.tenant, data=request.POST, files=request.FILES)
        if form.is_valid():
            excel_file = form.cleaned_data["excel_file"]
            selected_store = form.cleaned_data.get("store")

            # Gather any existing pending order_sns & product goods_ids
            existing_pending_sns = set()
            existing_goods_ids = set()
            try:
                from apps.orders.models import Order
                from apps.products.models import Product

                existing_pending_sns = set(
                    Order.objects.filter(
                        store__tenant=request.tenant,
                        settlement_status="PENDING_SETTLEMENT",
                    ).values_list("order_sn", flat=True)
                )
                existing_goods_ids = set(
                    Product.objects.filter(store__tenant=request.tenant).values_list("goods_id", flat=True)
                )
            except Exception:
                pass

            parser = KilimallWorkbookParser()
            parsed = parser.parse_file(
                excel_file,
                filename=excel_file.name,
                existing_pending_order_sns=existing_pending_sns,
                existing_product_goods_ids=existing_goods_ids,
            )

            if not parsed.is_valid:
                for err in parsed.errors:
                    messages.error(request, err)
                return render(request, self.template_name, {"form": form, "preview": parsed.to_dict()})

            # If vendor explicitly chose a store in the dropdown, verify it matches the workbook's Store ID
            if selected_store and parsed.kilimall_store_id and selected_store.kilimall_store_id != parsed.kilimall_store_id:
                messages.warning(
                    request,
                    f"Note: You selected '{selected_store.store_name}' ({selected_store.kilimall_store_id}), "
                    f"but this Excel file belongs to Store ID {parsed.kilimall_store_id} ({parsed.primary_store_name}). "
                    f"We have matched it to Store ID {parsed.kilimall_store_id}.",
                )

            request.session["pending_statement_preview"] = parsed.to_dict()
            messages.success(
                request,
                f"Workbook '{excel_file.name}' validated across all 9 sheets! Review the reconciliation impact below before committing.",
            )
            return redirect("statements:upload_wizard")

        return render(request, self.template_name, {"form": form, "preview": None})


class StatementLedgerView(VendorRequiredMixin, View):
    """
    Implements SCR-06: Settlement Statements Ledger, Upload History, and Statement Rollback.
    Respects the Global Store Context Switcher (`All My Stores` vs individual store).
    """

    template_name = "statements/statement_list.html"

    def get(self, request):
        qs = SettlementStatement.objects.filter(store__tenant=request.tenant).select_related("store", "uploaded_by")
        if request.active_store_scope and request.active_store_scope != "all":
            qs = qs.filter(store__kilimall_store_id=request.active_store_scope)

        search_q = request.GET.get("q", "").strip()
        if search_q:
            qs = qs.filter(filename__icontains=search_q) | qs.filter(store__store_name__icontains=search_q)

        totals = qs.aggregate(
            gross_sum=Sum("total_volume"),
            comm_sum=Sum("total_commission"),
            ds_sum=Sum("ds_processing_fee"),
            fines_sum=Sum("fines"),
            payout_sum=Sum("final_settlement_amount"),
            pending_sum=Sum("new_pending_settlement_count"),
        )

        return render(
            request,
            self.template_name,
            {
                "statements": qs,
                "totals": totals,
                "search_q": search_q,
            },
        )

    def post(self, request):
        # Rollback / Delete a Settlement Statement atomically (Vendor Owner only)
        if not request.user.can_manage_stores_and_rollback:
            messages.error(request, "Only the Vendor Owner can rollback an imported settlement statement.")
            return redirect("statements:statement_list")

        stmt_id = request.POST.get("statement_id")
        purge_unpriced = request.POST.get("purge_unpriced_products") == "1"
        stmt = get_object_or_404(SettlementStatement, pk=stmt_id, store__tenant=request.tenant)
        filename = stmt.filename
        store_name = stmt.store.store_name

        from services.reconciler import rollback_statement

        rb_stats = rollback_statement(
            statement=stmt,
            purge_unreferenced_unpriced_products=purge_unpriced,
        )
        messages.warning(
            request,
            (
                f"Rolled back statement '{filename}' for {store_name}: removed {rb_stats['orders_deleted']} "
                f"statement-exclusive orders, reverted {rb_stats['orders_reverted_to_pending']} cross-week orders "
                f"back to Pending Settlement, and deleted {rb_stats['logistics_fees_deleted']} DS fee records "
                f"and {rb_stats['penalties_deleted']} SLA fine records (wholesale COGS preserved)."
            ),
        )
        return redirect("statements:statement_list")


class StatementDetailView(VendorRequiredMixin, View):
    """
    Implements SCR-07: Single Settlement Statement Detail & Two-Stage Payout Audit View.
    Acts as the authoritative digital twin of an uploaded Kilimall workbook (`bill`, `bill details`,
    `ds processing fee`, `fine`, and `discovered_products`).
    """

    template_name = "statements/statement_detail.html"

    def get(self, request, pk):
        statement = get_object_or_404(
            SettlementStatement.objects.select_related("store", "uploaded_by"),
            pk=pk,
            store__tenant=request.tenant,
        )
        payload = statement.parsed_payload or {}
        active_tab = request.GET.get("tab", "sales")
        ds_filter = request.GET.get("ds_status", "all")

        ds_fees_list = payload.get("ds_fees", [])
        if ds_filter == "settled":
            ds_fees_list = [r for r in ds_fees_list if r.get("reconciliation_status") == "SETTLED"]
        elif ds_filter == "pending":
            ds_fees_list = [r for r in ds_fees_list if r.get("reconciliation_status") == "PENDING_SETTLEMENT"]

        return render(
            request,
            self.template_name,
            {
                "statement": statement,
                "payload": payload,
                "completed_sales": payload.get("completed_sales", []),
                "ds_fees": ds_fees_list,
                "fines": payload.get("fines", []),
                "other_deductions": payload.get("other_deductions", []),
                "discovered_products": payload.get("discovered_products", []),
                "sheet_checklist": payload.get("sheet_checklist", []),
                "impact": payload.get("reconciliation_impact", {}),
                "active_tab": active_tab,
                "ds_filter": ds_filter,
            },
        )
