"""
Views for Master Order Reconciliation Workspace & Order 360° Slide-Over Drawer (SCR-09),
Pending Settlement & DS Transit Escrow Tracker (SCR-10),
and DS Processing Fee & Penalties Monitoring Center (SCR-13).
"""
from decimal import Decimal
from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.accounts.middleware import VendorRequiredMixin
from apps.orders.models import LogisticsFee, Order, PenaltyFine
from services.reconciler import build_order_360_timeline, refresh_pending_order_aging


class OrderReconciliationWorkspaceView(VendorRequiredMixin, View):
    """
    Implements SCR-09: Master Order Reconciliation Workspace & Order 360° Split/Drawer View.
    Strictly separates `Settlement Status` (`Settled`, `Pending Settlement`, `Overdue Pending`)
    from `Penalty Status` (`No Penalty`, `Overdue Dispatch -300 KES`, `Out-of-Stock -300 KES`).
    """

    template_name = "orders/reconciliation_grid.html"

    def get(self, request):
        refresh_pending_order_aging(request.tenant)

        base_qs = Order.objects.filter(store__tenant=request.tenant).select_related(
            "store", "settlement_statement", "first_seen_statement"
        )
        if request.active_store_scope and request.active_store_scope != "all":
            base_qs = base_qs.filter(store__kilimall_store_id=request.active_store_scope)

        # Pipeline Counts (Across all orders in active store scope)
        pipeline_counts = {
            "all_count": base_qs.count(),
            "settled_count": base_qs.filter(settlement_status=Order.STATUS_SETTLED).count(),
            "same_bill_settled_count": base_qs.filter(
                settlement_status=Order.STATUS_SETTLED, ds_match_type=Order.DS_MATCH_SAME_BILL
            ).count(),
            "prior_ds_settled_count": base_qs.filter(
                settlement_status=Order.STATUS_SETTLED
            ).exclude(ds_match_type=Order.DS_MATCH_SAME_BILL).count(),
            "pending_count": base_qs.filter(settlement_status=Order.STATUS_PENDING).count(),
            "overdue_count": base_qs.filter(settlement_status=Order.STATUS_OVERDUE).count(),
            "penalized_count": base_qs.exclude(penalty_status=Order.PENALTY_NONE).count(),
            "settled_gross_sum": base_qs.filter(settlement_status=Order.STATUS_SETTLED).aggregate(
                s=Sum("goods_price")
            )["s"] or Decimal("0.00"),
            "pending_ds_sum": base_qs.filter(
                settlement_status__in=[Order.STATUS_PENDING, Order.STATUS_OVERDUE]
            ).aggregate(s=Sum("linked_ds_fee"))["s"] or Decimal("0.00"),
            "fines_sum": base_qs.exclude(penalty_status=Order.PENALTY_NONE).aggregate(
                s=Sum("linked_fine_amount")
            )["s"] or Decimal("0.00"),
        }

        # Apply Filters
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

        # Select an order for the right-hand 360° Inspector Panel (defaults to 610287144669824 if present or first settled order)
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
    HTMX endpoint (`/orders/<order_sn>/drawer/`) that loads the Order 360° Lifecycle Slide-Over Drawer
    into `#drawer-container` when clicking any Order SN across the platform.
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


class PendingShipmentsTrackerView(VendorRequiredMixin, View):
    """
    Implements SCR-10: Pending Settlement & Unsettled DS Shipments Aging Tracker.
    Tracks packages where Kilimall has deducted a DS Processing Fee (KES 50–250), grouped into aging buckets
    (0–7d, 8–14d, >14d Overdue), and celebrates orders that automatically flipped from Pending -> Settled.
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

        # Recently Auto-Settled Orders (Prior-week DS orders that settled in the latest upload)
        auto_settled_orders = base_qs.filter(
            settlement_status=Order.STATUS_SETTLED,
        ).exclude(ds_match_type=Order.DS_MATCH_SAME_BILL)
        auto_settled_gross = auto_settled_orders.aggregate(s=Sum("goods_price"))["s"] or Decimal("0.00")

        # Classify pending orders into aging buckets
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
        # Manual Exception Resolution: Mark a Pending/Overdue parcel as Returned to Seller or Restore to Pending
        order_sn = request.POST.get("order_sn")
        resolution = request.POST.get("resolution_action")
        notes = request.POST.get("resolution_notes", "").strip()

        order = get_object_or_404(Order, store__tenant=request.tenant, order_sn=order_sn)
        if resolution == "mark_returned":
            order.settlement_status = Order.STATUS_RETURNED
            order.resolution_notes = notes or "Confirmed physical return by courier; DS fee logged as sunk logistics cost."
            order.save(update_fields=["settlement_status", "resolution_notes", "updated_at"])
            messages.warning(
                request,
                f"Order {order.order_sn} (Shipment {order.linked_shipment_no}) marked as Returned to Seller (-KES {order.linked_ds_fee} DS fee recognized).",
            )
        elif resolution == "restore_pending":
            order.settlement_status = Order.STATUS_PENDING
            order.save(update_fields=["settlement_status", "updated_at"])
            messages.info(request, f"Order {order.order_sn} restored to active Pending Settlement monitoring.")

        return redirect("orders:pending_tracker")


class FeePenaltyMonitoringCenterView(VendorRequiredMixin, View):
    """
    Implements SCR-13: DS Processing Fee & Penalties Monitoring Center.
    - Section A: Audits all 5 Drop-Shipping package size tiers (`Super Small @ 50` to `Super Large @ 250`)
      and flags package size overcharge anomalies (e.g., Order `610272230744554` billed `Super Large @ KES 250` on a `KES 378` item).
    - Section B: Audits all SLA penalty fines (`Seller overdue dispatch` vs `Seller confirmed out of stock`),
      displaying cleaned elapsed dispatch delays (`7d 03h` to `13d 00h`) and store alias attribution (`Harambee Ⅰ`).
    """

    template_name = "orders/fees_penalties_audit.html"

    def get(self, request):
        fees_qs = LogisticsFee.objects.filter(order__store__tenant=request.tenant).select_related(
            "order", "statement", "order__store"
        )
        fines_qs = PenaltyFine.objects.filter(order__store__tenant=request.tenant).select_related(
            "order", "statement", "order__store"
        )

        if request.active_store_scope and request.active_store_scope != "all":
            fees_qs = fees_qs.filter(order__store__kilimall_store_id=request.active_store_scope)
            fines_qs = fines_qs.filter(order__store__kilimall_store_id=request.active_store_scope)

        # Package Size Tier Breakdown
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

        # Package Size Anomaly Orders (DS fee >= 35% of selling price)
        size_anomalies = Order.objects.filter(
            store__tenant=request.tenant,
            has_package_size_anomaly=True,
        ).select_related("store", "settlement_statement")
        if request.active_store_scope and request.active_store_scope != "all":
            size_anomalies = size_anomalies.filter(store__kilimall_store_id=request.active_store_scope)

        # Fines & Penalties Breakdown
        total_fines_amount = fines_qs.aggregate(s=Sum("fine_amount"))["s"] or Decimal("0.00")
        overdue_dispatch_fines = fines_qs.filter(penalty_code=Order.PENALTY_OVERDUE_DISPATCH)
        out_of_stock_fines = fines_qs.filter(penalty_code=Order.PENALTY_OUT_OF_STOCK)

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
                "active_section": active_section,
            },
        )
