"""
Decoupled Financial Calculation, Dual-Basis P&L & Chart.js Dataset Engine (`services/financials.py`).

Implements Section 2.5 and Section 8.3 of the KiliLedger Pro Blueprint:
1. Supports switching seamlessly between:
   - `CASH_FLOW` (Confirmed Default for SCR-03): Reports exact statement cash deductions (`-KES 2,300` DS fees
     across 22 packages, `-KES 1,800` fines across 6 incidents) to match the `KES 16,486.78` bank deposit to the cent,
     then subtracts verified COGS of settled units to compute `Estimated Pocket Profit`.
   - `ORDER_ACCRUAL` (No Double-Counting Unit Economics): Deducts ONLY the DS processing fees linked to the
     completed orders sold in the period to compute `Gross Order Contribution Profit`, and isolates unlinked
     store-level penalties (`-KES 1,800`) and Pending Escrow DS timing differences (`-KES 1,490` across 15 packages)
     on separate lines below contribution margin.
2. Aggregates across either a single Kilimall store (`?store=10344`) or `All My Stores` (`?store=all`).
3. Builds ready-to-render Chart.js 4.x JSON datasets for:
   - Gross Sales vs. Net Kilimall Payout vs. True Profit Over Time (Combo Bar + Line Chart)
   - Gross Revenue to Pocket Profit Waterfall / Horizontal Breakdown
   - Commission Rate Tier Distribution (`9%`, `12%`, `15%`, `16%`)
   - Cross-Week Order Reconciliation Pipeline Donut (`Settled`, `Pending 0–7d`, `Pending 8–14d`, `Overdue >14d`, `Penalized`)
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List

from django.db.models import Count, Q, Sum

from apps.orders.models import Order
from apps.products.models import Product
from apps.statements.models import SettlementStatement
from apps.stores.models import Store


def _pct_of(part: Decimal, whole: Decimal) -> Decimal:
    if not whole or whole <= 0:
        return Decimal("0.00")
    return (abs(part) / whole * Decimal("100")).quantize(Decimal("0.01"))


def compute_dashboard_and_pnl_metrics(
    tenant,
    store_scope: str = "all",
    accounting_basis: str = "CASH_FLOW",
) -> Dict[str, Any]:
    """
    Computes the complete 8-card KPI suite, Dual-Basis P&L Statement table, Multi-Store Comparative
    Benchmark Matrix, and Chart.js datasets for `SCR-03` and `SCR-08`.
    """
    stores_qs = Store.objects.filter(tenant=tenant, is_active=True)
    active_store = None
    if store_scope and store_scope != "all":
        active_store = stores_qs.filter(kilimall_store_id=store_scope).first()

    stmt_qs = SettlementStatement.objects.filter(store__tenant=tenant).select_related("store").order_by("period_end")
    orders_qs = Order.objects.filter(store__tenant=tenant).select_related("store")
    products_qs = Product.objects.filter(store__tenant=tenant).select_related("store")

    if active_store:
        stmt_qs = stmt_qs.filter(store=active_store)
        orders_qs = orders_qs.filter(store=active_store)
        products_qs = products_qs.filter(store=active_store)

    # 1. Statement-Level Cash Flow Totals (Matches 'bill' worksheet summary to the cent)
    stmt_aggs = stmt_qs.aggregate(
        gross_sales=Sum("total_volume"),
        total_commission=Sum("total_commission"),
        quality_fee=Sum("quality_inspection_fee"),
        payable_1=Sum("settlement_payable_1"),
        statement_ds_fees=Sum("ds_processing_fee"),
        statement_fines=Sum("fines"),
        other_deductions=Sum("other_deductions"),
        compensations=Sum("compensations"),
        net_kilimall_payout=Sum("final_settlement_amount"),
    )

    gross_sales = stmt_aggs["gross_sales"] or Decimal("0.00")
    total_commission = stmt_aggs["total_commission"] or Decimal("0.00")          # Negative (e.g., -3673.22)
    payable_1 = stmt_aggs["payable_1"] or Decimal("0.00")                        # e.g., 20586.78
    statement_ds_fees = stmt_aggs["statement_ds_fees"] or Decimal("0.00")        # Negative (e.g., -2300.00)
    statement_fines = stmt_aggs["statement_fines"] or Decimal("0.00")            # Negative (e.g., -1800.00)
    net_kilimall_payout = stmt_aggs["net_kilimall_payout"] or Decimal("0.00")    # e.g., 16486.78

    # 2. Order-Level Accrual & COGS Metrics
    settled_orders = orders_qs.filter(settlement_status=Order.STATUS_SETTLED)
    pending_orders = orders_qs.filter(settlement_status__in=[Order.STATUS_PENDING, Order.STATUS_OVERDUE])
    penalized_orders = orders_qs.exclude(penalty_status=Order.PENALTY_NONE)

    settled_units_count = settled_orders.aggregate(u=Sum("goods_num"))["u"] or 0
    priced_orders = settled_orders.filter(applied_cogs__isnull=False)
    priced_units_count = priced_orders.aggregate(u=Sum("goods_num"))["u"] or 0
    unpriced_units_count = max(0, settled_units_count - priced_units_count)

    total_cogs = priced_orders.aggregate(c=Sum("applied_cogs"))["c"] or Decimal("0.00")

    # Matched DS fees on settled orders (for Order-Matched Accrual Basis)
    same_bill_matched_ds = settled_orders.aggregate(d=Sum("linked_ds_fee"))["d"] or Decimal("0.00")
    # For the 9 orders in bill (55).xlsx whose DS fee was charged in a prior bill not yet uploaded,
    # estimate their standard Medium DS fee (KES 100) only in Order-Accrual mode if linked_ds_fee is 0
    prior_unlinked_settled_count = settled_orders.filter(linked_ds_fee=0).count()
    accrual_matched_ds_fees = same_bill_matched_ds + (Decimal("100.00") * prior_unlinked_settled_count)

    # Pending Settlement Pipeline Value
    pending_orders_count = pending_orders.count()
    pending_ds_fees_locked = pending_orders.aggregate(d=Sum("linked_ds_fee"))["d"] or Decimal("0.00")
    avg_order_value = (gross_sales / Decimal(settled_units_count)).quantize(Decimal("0.01")) if settled_units_count > 0 else Decimal("1200.00")
    estimated_pending_gross_value = (avg_order_value * pending_orders_count).quantize(Decimal("0.01"))

    # 3. Dual-Basis Profit Calculation
    # A) CASH-FLOW BASIS (Confirmed Default on SCR-03):
    #    Cash Profit = Net Kilimall Payout (16,486.78) - Total Verified COGS
    cash_basis_profit = (net_kilimall_payout - total_cogs).quantize(Decimal("0.01"))
    cash_basis_margin_pct = (
        (cash_basis_profit / gross_sales * Decimal("100")).quantize(Decimal("0.01"))
        if gross_sales > 0
        else Decimal("0.00")
    )

    # B) ORDER-MATCHED ACCRUAL BASIS (SCR-08 & Toggle):
    #    Order Contribution Profit = Gross Sales (24,260) - |Commission| (3,673.22) - Accrual Matched DS Fees - Total COGS
    #    Operating Net Profit (Accrual) = Order Contribution Profit - |Store Fines| (1,800.00)
    net_platform_order_revenue = (gross_sales - abs(total_commission) - accrual_matched_ds_fees).quantize(Decimal("0.01"))
    order_contribution_profit = (net_platform_order_revenue - total_cogs).quantize(Decimal("0.01"))
    order_contribution_margin_pct = (
        (order_contribution_profit / gross_sales * Decimal("100")).quantize(Decimal("0.01"))
        if gross_sales > 0
        else Decimal("0.00")
    )
    accrual_operating_profit = (order_contribution_profit - abs(statement_fines)).quantize(Decimal("0.01"))
    accrual_operating_margin_pct = (
        (accrual_operating_profit / gross_sales * Decimal("100")).quantize(Decimal("0.01"))
        if gross_sales > 0
        else Decimal("0.00")
    )

    # Timing difference between DS fees deducted in statement (-2,300) vs matched to settled sales
    ds_timing_adjustment = (abs(statement_ds_fees) - accrual_matched_ds_fees).quantize(Decimal("0.01"))

    # Active displayed profit based on user's selected accounting basis
    if accounting_basis == "ORDER_ACCRUAL":
        displayed_ds_fees = -accrual_matched_ds_fees
        displayed_profit = accrual_operating_profit
        displayed_margin_pct = accrual_operating_margin_pct
        basis_label = "Order-Matched Accrual Basis"
    else:
        displayed_ds_fees = statement_ds_fees
        displayed_profit = cash_basis_profit
        displayed_margin_pct = cash_basis_margin_pct
        basis_label = "Settlement Cash-Flow Basis"

    # 4. Product COGS Completeness & Top/Bottom SKUs
    total_skus_count = products_qs.count()
    missing_cogs_skus_count = products_qs.filter(current_buying_price__isnull=True).count()
    is_profit_incomplete = missing_cogs_skus_count > 0 or unpriced_units_count > 0

    # 5. Commission Tier Breakdown (9%, 12%, 15%, 16%)
    comm_tiers_raw = (
        settled_orders.values("commission_rate")
        .annotate(order_count=Count("id"), sales_sum=Sum("goods_price"), comm_sum=Sum("commission_amount"))
        .order_by("-commission_rate")
    )
    commission_tiers = []
    for row in comm_tiers_raw:
        r_dec = row["commission_rate"] or Decimal("0.00")
        r_pct = (r_dec * Decimal("100")).quantize(Decimal("0.1"))
        s_sum = row["sales_sum"] or Decimal("0.00")
        commission_tiers.append(
            {
                "rate_pct": str(r_pct),
                "order_count": row["order_count"],
                "sales_sum": s_sum,
                "comm_sum": row["comm_sum"] or Decimal("0.00"),
                "share_of_gmv_pct": _pct_of(s_sum, gross_sales),
            }
        )

    # 6. Multi-Store Comparative Benchmark Matrix (for 'All My Stores' & SCR-08 Tab B)
    store_benchmarks = []
    for st in stores_qs:
        st_stmts = SettlementStatement.objects.filter(store=st)
        st_orders = Order.objects.filter(store=st)
        st_agg = st_stmts.aggregate(
            gmv=Sum("total_volume"),
            comm=Sum("total_commission"),
            ds=Sum("ds_processing_fee"),
            fines=Sum("fines"),
            payout=Sum("final_settlement_amount"),
        )
        st_gmv = st_agg["gmv"] or Decimal("0.00")
        st_payout = st_agg["payout"] or Decimal("0.00")
        st_cogs = (
            st_orders.filter(settlement_status=Order.STATUS_SETTLED, applied_cogs__isnull=False).aggregate(
                c=Sum("applied_cogs")
            )["c"]
            or Decimal("0.00")
        )
        st_profit = (st_payout - st_cogs).quantize(Decimal("0.01"))
        st_retention = _pct_of(st_payout, st_gmv)
        st_margin = (st_profit / st_gmv * Decimal("100")).quantize(Decimal("0.01")) if st_gmv > 0 else Decimal("0.00")

        store_benchmarks.append(
            {
                "store": st,
                "statements_count": st_stmts.count(),
                "gross_sales": st_gmv,
                "commission": st_agg["comm"] or Decimal("0.00"),
                "ds_fees": st_agg["ds"] or Decimal("0.00"),
                "fines": st_agg["fines"] or Decimal("0.00"),
                "net_payout": st_payout,
                "cogs": st_cogs,
                "net_profit": st_profit,
                "retention_pct": st_retention,
                "margin_pct": st_margin,
                "pending_count": st_orders.filter(settlement_status=Order.STATUS_PENDING).count(),
            }
        )

    # 7. Build Chart.js 4.x Datasets
    # Chart 1: Time-Series Trend across statements (or single cycle + historical baseline)
    trend_labels = []
    trend_gross = []
    trend_payout = []
    trend_profit = []

    for stmt in stmt_qs:
        cycle_label = f"{stmt.period_end:%b %d} ({stmt.store.kilimall_store_id})"
        stmt_cogs = (
            Order.objects.filter(settlement_statement=stmt, applied_cogs__isnull=False).aggregate(
                c=Sum("applied_cogs")
            )["c"]
            or Decimal("0.00")
        )
        trend_labels.append(cycle_label)
        trend_gross.append(float(stmt.total_volume))
        trend_payout.append(float(stmt.final_settlement_amount))
        trend_profit.append(float(stmt.final_settlement_amount - stmt_cogs))

    if not trend_labels:
        trend_labels = ["No Statements Uploaded"]
        trend_gross = [0]
        trend_payout = [0]
        trend_profit = [0]

    # Chart 2: Revenue & Deduction Waterfall Breakdown
    waterfall_labels = [
        "Gross Sales",
        "Commissions",
        "DS Fees",
        "SLA Fines",
        "Net Payout",
        "Verified COGS",
        "Pocket Profit",
    ]
    waterfall_values = [
        float(gross_sales),
        float(abs(total_commission)),
        float(abs(displayed_ds_fees)),
        float(abs(statement_fines)),
        float(net_kilimall_payout),
        float(total_cogs),
        float(displayed_profit),
    ]

    # Chart 3: Order Reconciliation Pipeline Donut
    all_pending_list = list(pending_orders)
    pending_0_7 = sum(1 for o in all_pending_list if o.aging_bucket == "NORMAL_0_7D")
    pending_8_14 = sum(1 for o in all_pending_list if o.aging_bucket == "EXTENDED_8_14D")
    pending_overdue = sum(1 for o in all_pending_list if o.aging_bucket == "OVERDUE")

    pipeline_donut = {
        "settled": settled_orders.count(),
        "pending_0_7d": pending_0_7,
        "pending_8_14d": pending_8_14,
        "overdue": pending_overdue,
        "penalized": penalized_orders.count(),
    }

    chart_payload_json = json.dumps(
        {
            "trend": {
                "labels": trend_labels,
                "gross": trend_gross,
                "payout": trend_payout,
                "profit": trend_profit,
            },
            "waterfall": {
                "labels": waterfall_labels,
                "values": waterfall_values,
            },
            "pipeline": pipeline_donut,
        }
    )

    return {
        "accounting_basis": accounting_basis,
        "basis_label": basis_label,
        "gross_sales": gross_sales,
        "total_commission": total_commission,
        "commission_pct_of_gross": _pct_of(total_commission, gross_sales),
        "payable_1": payable_1,
        "payable_1_pct_of_gross": _pct_of(payable_1, gross_sales),
        "statement_ds_fees": statement_ds_fees,
        "accrual_matched_ds_fees": -accrual_matched_ds_fees,
        "displayed_ds_fees": displayed_ds_fees,
        "ds_fees_pct_of_gross": _pct_of(displayed_ds_fees, gross_sales),
        "statement_fines": statement_fines,
        "fines_pct_of_gross": _pct_of(statement_fines, gross_sales),
        "net_kilimall_payout": net_kilimall_payout,
        "payout_retention_pct": _pct_of(net_kilimall_payout, gross_sales),
        "total_cogs": total_cogs,
        "cogs_pct_of_gross": _pct_of(total_cogs, gross_sales),
        "settled_units_count": settled_units_count,
        "priced_units_count": priced_units_count,
        "unpriced_units_count": unpriced_units_count,
        "total_skus_count": total_skus_count,
        "missing_cogs_skus_count": missing_cogs_skus_count,
        "is_profit_incomplete": is_profit_incomplete,
        # Dual-Basis Profit Metrics
        "cash_basis_profit": cash_basis_profit,
        "cash_basis_margin_pct": cash_basis_margin_pct,
        "net_platform_order_revenue": net_platform_order_revenue,
        "order_contribution_profit": order_contribution_profit,
        "order_contribution_margin_pct": order_contribution_margin_pct,
        "accrual_operating_profit": accrual_operating_profit,
        "accrual_operating_margin_pct": accrual_operating_margin_pct,
        "ds_timing_adjustment": ds_timing_adjustment,
        "displayed_profit": displayed_profit,
        "displayed_margin_pct": displayed_margin_pct,
        # Pipeline & Benchmarks
        "pending_orders_count": pending_orders_count,
        "pending_ds_fees_locked": pending_ds_fees_locked,
        "estimated_pending_gross_value": estimated_pending_gross_value,
        "penalized_orders_count": penalized_orders.count(),
        "pipeline_donut": pipeline_donut,
        "commission_tiers": commission_tiers,
        "store_benchmarks": store_benchmarks,
        "chart_payload_json": chart_payload_json,
    }
