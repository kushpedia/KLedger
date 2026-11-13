"""
Views for Master Order Reconciliation Workspace & Order 360° Slide-Over Drawer (SCR-09),
Pending Settlement & DS Transit Escrow Tracker (SCR-10),
DS Processing Fee, SLA Fines & Other Deductions Monitoring Center (SCR-13),
and Customer Return Condition Toggle (`RESTOCKED` vs. `DAMAGED_LOST`).
"""
from decimal import Decimal
from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.accounts.middleware import VendorRequiredMixin
from apps.orders.models import LogisticsFee, Order, OtherDeduction, PenaltyFine
from apps.products.models import Product
from services.reconciler import build_order_360_timeline, refresh_pending_order_aging


class OrderReconciliationWorkspaceView(VendorRequiredMixin, View):
    """
    Implements SCR-09: Master Order Reconciliation Workspace & Order 360° Split/Drawer View.
    """

    template_name = "orders/reconciliation_grid.html"

    def get(self, request):
        refresh_pending_order_aging(request.tenant)

        base_qs = Order.objects.filter(store__tenant=request.tenant).select_related(
            "store", "settlement_statement", "first_seen_statement"
        )
        if request.active_store_scope and request.active_store_scope != "all":
            base_qs = base_qs.filter(store__kilimall_store_id=request.active_store_scope)

        returned_qs = base_qs.filter(settlement_status=Order.STATUS_RETURNED)

        fines_sum = base_qs.exclude(penalty_status=Order.PENALTY_NONE).aggregate(
            s=Sum("linked_fine_amount")
        )["s"] or Decimal("0.00")
        other_ded_sum = base_qs.aggregate(s=Sum("linked_other_deduction"))["s"] or Decimal("0.00")

        pipeline_counts = {
            "all_count": base_qs.count(),
            "settled_count": base_qs.filter(settlement_status=Order.STATUS_SETTLED).count(),
            "same_bill_settled_count": base_qs.filter(
                settlement_status=Order.STATUS_SETTLED, ds_match_type=Order.DS_MATCH_SAME_BILL
            ).count(),
            "prior_ds_settled_count": base_qs.filter(
                settlement_status=Order.STATUS_SETTLED
            ).exclude(ds_match_type=Order.DS_MATCH_SAME_BILL).count(),
            "returned_count": returned_qs.count(),
            "restocked_returns_count": returned_qs.filter(return_condition=Order.RETURN_COND_RESTOCKED).count(),
            "damaged_returns_count": returned_qs.filter(return_condition=Order.RETURN_COND_DAMAGED).count(),
            "returned_clawback_sum": returned_qs.aggregate(s=Sum("net_settlement"))["s"] or Decimal("0.00"),
            "pending_count": base_qs.filter(settlement_status=Order.STATUS_PENDING).count(),
            "overdue_count": base_qs.filter(settlement_status=Order.STATUS_OVERDUE).count(),
            "penalized_count": base_qs.exclude(penalty_status=Order.PENALTY_NONE).count(),
            "settled_gross_sum": base_qs.filter(settlement_status=Order.STATUS_SETTLED).aggregate(
                s=Sum("complete_amount")
            )["s"]
            or base_qs.filter(settlement_status=Order.STATUS_SETTLED).aggregate(s=Sum("goods_price"))["s"]
            or Decimal("0.00"),
            "pending_ds_sum": base_qs.filter(
                settlement_status__in=[Order.STATUS_PENDING, Order.STATUS_OVERDUE]
            ).aggregate(s=Sum("linked_ds_fee"))["s"] or Decimal("0.00"),
            "fines_sum": (fines_sum + other_ded_sum).quantize(Decimal("0.01")),
        }

        status_filter = request.GET.get("status", "all")
        penalty_filter = request.GET.get("penalty", "all")
        search_q = request.GET.get("q", "").strip()

        filtered_qs = base_qs
        if status_filter == "settled":
            filtered_qs = filtered_qs.filter(settlement_status=Order.STATUS_SETTLED)
        elif status_filter == "pending":
            filtered_qs = filtered_qs.filter(settlement_status=Order.STATUS_PENDING)
        elif status_filter == "overdue":
            filtered_qs = filtered_qs.filter(settlement_status=Order.STATUS_OVERDUE)
        elif status_filter == "penalized":
            filtered_qs = filtered_qs.exclude(penalty_status=Order.PENALTY_NONE)
        elif status_filter == "returned":
            filtered_qs = filtered_qs.filter(settlement_status=Order.STATUS_RETURNED)

        if penalty_filter != "all":
            filtered_qs = filtered_qs.filter(penalty_status=penalty_filter)

        if search_q:
            filtered_qs = filtered_qs.filter(
                Q(order_sn__icontains=search_q)
                | Q(linked_shipment_no__icontains=search_q)
                | Q(goods_id__icontains=search_q)
                | Q(headline_name__icontains=search_q)
            )

        selected_sn = request.GET.get("inspect")
        selected_order = None
        if selected_sn:
            selected_order = base_qs.filter(order_sn=selected_sn).first()
        if not selected_order:
            selected_order = (
                base_qs.filter(order_sn="610287144669824").first()
                or filtered_qs.first()
                or base_qs.first()
            )

        selected_timeline = build_order_360_timeline(selected_order) if selected_order else []

        return render(
            request,
            self.template_name,
            {
                "orders": filtered_qs[:100],
                "pipeline": pipeline_counts,
                "status_filter": status_filter,
                "penalty_filter": penalty_filter,
                "search_q": search_q,
                "selected_order": selected_order,
                "selected_timeline": selected_timeline,
            },
        )


class Order360DrawerPartialView(VendorRequiredMixin, View):
    """
    HTMX endpoint (`/orders/<order_sn>/drawer/`) that loads the Order 360° Lifecycle Slide-Over Drawer.
    """

    template_name = "orders/_order_360_drawer.html"

    def get(self, request, order_sn):
        order = get_object_or_404(
            Order.objects.select_related("store", "settlement_statement", "first_seen_statement"),
            store__tenant=request.tenant,
            order_sn=order_sn,
        )
        timeline = build_order_360_timeline(order)
        return render(
            request,
            self.template_name,
            {
                "order": order,
                "timeline": timeline,
            },
        )


class OrderReturnConditionToggleView(VendorRequiredMixin, View):
    """
    POST / HTMX endpoint (`/orders/<order_sn>/return-condition/`) allowing Store Owners, Finance Auditors,
    and Operations Assistants to toggle a returned item (`goods_num < 0` or `STATUS_RETURNED`) between:
    - `RESTOCKED` (Resellable / Returned to Inventory -> COGS recovered)
    - `DAMAGED_LOST` (Damaged / Unsellable -> Wholesale COGS is written off as a loss)
    """

    def post(self, request, order_sn):
        order = get_object_or_404(
            Order.objects.select_related("store", "settlement_statement", "first_seen_statement"),
            store__tenant=request.tenant,
            order_sn=order_sn,
        )
        new_cond = request.POST.get("return_condition", Order.RETURN_COND_RESTOCKED)
        if new_cond not in (Order.RETURN_COND_RESTOCKED, Order.RETURN_COND_DAMAGED):
            new_cond = Order.RETURN_COND_RESTOCKED

        if order.unit_buying_price is None and order.goods_id:
            prod = Product.objects.filter(store=order.store, goods_id=order.goods_id).first()
            if prod and prod.current_buying_price is not None:
                order.unit_buying_price = prod.current_buying_price

        order.settlement_status = Order.STATUS_RETURNED
        order.return_condition = new_cond
        order.recalculate_profit()
        order.save()

        cond_display = (
            "Resellable / Restocked (COGS Recovered)"
            if new_cond == Order.RETURN_COND_RESTOCKED
            else f"Damaged / Lost (-KES {order.applied_cogs or Decimal('0.00'):,.2f} COGS Written Off)"
        )
        messages.success(
            request,
            f"Updated return condition for Order {order.order_sn} to: {cond_display}.",
        )

        if request.headers.get("HX-Request") == "true" and request.POST.get("context") == "drawer":
            timeline = build_order_360_timeline(order)
            return render(
                request,
                "orders/_order_360_drawer.html",
                {
                    "order": order,
                    "timeline": timeline,
                },
            )

        next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/orders/?status=returned"
        return redirect(next_url)


class PendingShipmentsTrackerView(VendorRequiredMixin, View):
    """
    Implements SCR-10: Pending Settlement & Unsettled DS Shipments Aging Tracker.
    """

    template_name = "orders/pending_tracker.html"

    def get(self, request):
        refresh_pending_order_aging(request.tenant)

        base_qs = Order.objects.filter(store__tenant=request.tenant).select_related(
            "store", "first_seen_statement", "settlement_statement"
        )
        if request.active_store_scope and request.active_store_scope != "all":
            base_qs = base_qs.filter(store__kilimall_store_id=request.active_store_scope)

        pending_qs = base_qs.filter(
            settlement_status__in=[Order.STATUS_PENDING, Order.STATUS_OVERDUE]
        ).order_by("ds_fee_scan_date")

        auto_settled_orders = base_qs.filter(
            settlement_status=Order.STATUS_SETTLED,
        ).exclude(ds_match_type=Order.DS_MATCH_SAME_BILL)
        auto_settled_gross = (
            auto_settled_orders.aggregate(s=Sum("complete_amount"))["s"]
            or auto_settled_orders.aggregate(s=Sum("goods_price"))["s"]
            or Decimal("0.00")
        )

        all_pending_list = list(pending_qs)
        bucket_0_7 = [o for o in all_pending_list if o.aging_bucket == "NORMAL_0_7D"]
        bucket_8_14 = [o for o in all_pending_list if o.aging_bucket == "EXTENDED_8_14D"]
        bucket_overdue = [o for o in all_pending_list if o.aging_bucket == "OVERDUE"]

        bucket_filter = request.GET.get("bucket", "all")
        pkg_filter = request.GET.get("package_size", "all")
        search_q = request.GET.get("q", "").strip()

        display_list = all_pending_list
        if bucket_filter == "0_7d":
            display_list = bucket_0_7
        elif bucket_filter == "8_14d":
            display_list = bucket_8_14
        elif bucket_filter == "overdue":
            display_list = bucket_overdue

        if pkg_filter != "all":
            display_list = [o for o in display_list if o.linked_package_size.lower() == pkg_filter.lower()]

        if search_q:
            q_low = search_q.lower()
            display_list = [
                o for o in display_list
                if q_low in o.order_sn.lower() or q_low in (o.linked_shipment_no or "").lower()
            ]

        total_pending_ds_fees = sum((o.linked_ds_fee for o in all_pending_list), Decimal("0.00"))

        return render(
            request,
            self.template_name,
            {
                "pending_orders": display_list,
                "total_pending_count": len(all_pending_list),
                "total_pending_ds_fees": total_pending_ds_fees,
                "bucket_0_7_count": len(bucket_0_7),
                "bucket_8_14_count": len(bucket_8_14),
                "bucket_overdue_count": len(bucket_overdue),
                "auto_settled_count": auto_settled_orders.count(),
                "auto_settled_gross": auto_settled_gross,
                "bucket_filter": bucket_filter,
                "pkg_filter": pkg_filter,
                "search_q": search_q,
                "aging_threshold_days": request.tenant.pending_aging_threshold_days,
            },
        )

    def post(self, request):
        order_sn = request.POST.get("order_sn")
        resolution = request.POST.get("resolution_action")
        return_cond = request.POST.get("return_condition", Order.RETURN_COND_RESTOCKED)
        notes = request.POST.get("resolution_notes", "").strip()

        order = get_object_or_404(Order, store__tenant=request.tenant, order_sn=order_sn)
        if resolution == "mark_returned":
            order.settlement_status = Order.STATUS_RETURNED
            order.return_condition = (
                return_cond
                if return_cond in (Order.RETURN_COND_RESTOCKED, Order.RETURN_COND_DAMAGED)
                else Order.RETURN_COND_RESTOCKED
            )
            order.resolution_notes = notes or "Confirmed physical return by courier; DS fee logged as sunk logistics cost."
            order.recalculate_profit()
            order.save()
            messages.warning(
                request,
                f"Order {order.order_sn} marked as Returned ({order.get_return_condition_display()}).",
            )
        elif resolution == "restore_pending":
            order.settlement_status = Order.STATUS_PENDING
            order.return_condition = Order.RETURN_COND_NA
            order.save(update_fields=["settlement_status", "return_condition", "updated_at"])
            messages.info(request, f"Order {order.order_sn} restored to active Pending Settlement monitoring.")

        return redirect("orders:pending_tracker")


class FeePenaltyMonitoringCenterView(VendorRequiredMixin, View):
    """
    Implements SCR-13: DS Processing Fee, SLA Fines & Other Deductions Monitoring Center.
    - Section A: Audits all 5 Drop-Shipping package size tiers (`Super Small @ 50` to `Super Large @ 250`).
    - Section B: Audits all SLA penalty fines (`Seller overdue dispatch`, `Seller confirmed out of stock`, `DS late arrival`).
    - Section C: Audits all `Other Deductions` rows (`violation` @ KES 500 & `After-sale return shipping` @ KES 200).
    """

    template_name = "orders/fees_penalties_audit.html"

    def get(self, request):
        fees_qs = LogisticsFee.objects.filter(order__store__tenant=request.tenant).select_related(
            "order", "statement", "order__store"
        )
        fines_qs = PenaltyFine.objects.filter(order__store__tenant=request.tenant).select_related(
            "order", "statement", "order__store"
        )
        other_ded_qs = OtherDeduction.objects.filter(order__store__tenant=request.tenant).select_related(
            "order", "statement", "order__store"
        )

        if request.active_store_scope and request.active_store_scope != "all":
            fees_qs = fees_qs.filter(order__store__kilimall_store_id=request.active_store_scope)
            fines_qs = fines_qs.filter(order__store__kilimall_store_id=request.active_store_scope)
            other_ded_qs = other_ded_qs.filter(order__store__kilimall_store_id=request.active_store_scope)

        tier_order = ["Super Small", "Small", "Medium", "Large", "Super Large"]
        tier_rates = {"Super Small": 50, "Small": 80, "Medium": 100, "Large": 200, "Super Large": 250}
        raw_tiers = fees_qs.values("package_size").annotate(pkg_count=Count("id"), fee_sum=Sum("amount"))
        tier_lookup = {r["package_size"]: r for r in raw_tiers}

        total_ds_pkgs = fees_qs.count()
        total_ds_amount = fees_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

        package_tiers = []
        for t_name in tier_order:
            info = tier_lookup.get(t_name, {"pkg_count": 0, "fee_sum": Decimal("0.00")})
            share_pct = (
                round((info["pkg_count"] / total_ds_pkgs) * 100, 1)
                if total_ds_pkgs > 0
                else 0.0
            )
            package_tiers.append(
                {
                    "name": t_name,
                    "unit_rate": tier_rates[t_name],
                    "pkg_count": info["pkg_count"],
                    "fee_sum": info["fee_sum"] or Decimal("0.00"),
                    "share_pct": share_pct,
                }
            )

        size_anomalies = Order.objects.filter(
            store__tenant=request.tenant,
            has_package_size_anomaly=True,
        ).select_related("store", "settlement_statement")
        if request.active_store_scope and request.active_store_scope != "all":
            size_anomalies = size_anomalies.filter(store__kilimall_store_id=request.active_store_scope)

        # Section B: Fines & Penalties Breakdown ('fine' sheet)
        total_fines_amount = fines_qs.aggregate(s=Sum("fine_amount"))["s"] or Decimal("0.00")
        overdue_dispatch_fines = fines_qs.filter(penalty_code=Order.PENALTY_OVERDUE_DISPATCH)
        out_of_stock_fines = fines_qs.filter(penalty_code=Order.PENALTY_OUT_OF_STOCK)
        ds_late_arrival_fines = fines_qs.filter(penalty_code=Order.PENALTY_DS_LATE_ARRIVAL)

        # Section C: Other Deductions Breakdown ('Other Deductions' sheet)
        total_other_deductions_amount = other_ded_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        violation_deductions = other_ded_qs.filter(deduction_code=OtherDeduction.CODE_VIOLATION)
        return_shipping_deductions = other_ded_qs.filter(deduction_code=OtherDeduction.CODE_RETURN_SHIPPING)

        active_section = request.GET.get("section", "all")

        return render(
            request,
            self.template_name,
            {
                "package_tiers": package_tiers,
                "total_ds_pkgs": total_ds_pkgs,
                "total_ds_amount": total_ds_amount,
                "size_anomalies": size_anomalies,
                "logistics_fees": fees_qs[:50],
                "fines": fines_qs,
                "total_fines_count": fines_qs.count(),
                "total_fines_amount": total_fines_amount,
                "overdue_dispatch_count": overdue_dispatch_fines.count(),
                "overdue_dispatch_sum": overdue_dispatch_fines.aggregate(s=Sum("fine_amount"))["s"] or Decimal("0.00"),
                "out_of_stock_count": out_of_stock_fines.count(),
                "out_of_stock_sum": out_of_stock_fines.aggregate(s=Sum("fine_amount"))["s"] or Decimal("0.00"),
                "ds_late_arrival_count": ds_late_arrival_fines.count(),
                "ds_late_arrival_sum": ds_late_arrival_fines.aggregate(s=Sum("fine_amount"))["s"] or Decimal("0.00"),
                # Section C: Other Deductions
                "other_deductions": other_ded_qs,
                "total_other_deductions_count": other_ded_qs.count(),
                "total_other_deductions_amount": total_other_deductions_amount,
                "violation_deductions_count": violation_deductions.count(),
                "violation_deductions_sum": violation_deductions.aggregate(s=Sum("amount"))["s"] or Decimal("0.00"),
                "return_shipping_count": return_shipping_deductions.count(),
                "return_shipping_sum": return_shipping_deductions.aggregate(s=Sum("amount"))["s"] or Decimal("0.00"),
                "active_section": active_section,
            },
        )
