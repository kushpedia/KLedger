"""
Cross-Week Order Reconciliation & Lifecycle Matching Engine (`services/reconciler.py`).

Responsibilities:
1. Executes `commit_statement_orders_and_products` atomically whenever a Kilimall `.xlsx` statement is committed:
   - Processes all `ds_fees` rows: creates or updates `Order` and `LogisticsFee` records.
   - Processes all `completed_sales` rows: matches existing `PENDING_SETTLEMENT` orders by `(store, order_sn)`
     and automatically flips them to `SETTLED` (setting `was_auto_flipped_from_pending=True`), or creates
     new `SETTLED` orders (`SAME_BILL` vs `PRIOR_CYCLE_DS`).
   - Processes all `fines` rows: creates `PenaltyFine` records and sets `penalty_status` (`OVERDUE_DISPATCH`
     vs `OUT_OF_STOCK`) without conflating penalties with completed sale settlements.
2. Evaluates configurable pending aging rules (default `14 days` from `ds_fee_scan_date`) to transition
   stale pending orders to `OVERDUE_PENDING`.
3. Builds the complete **Order 360° Cross-Statement Audit Timeline** for the slide-over drawer (`SCR-09`).
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Dict, List

from django.db import transaction
from django.utils import timezone

from apps.orders.models import LogisticsFee, Order, PenaltyFine
from apps.statements.models import SettlementStatement
from apps.stores.models import Store


def _to_aware_dt(dt_str: str | None) -> datetime.datetime | None:
    if not dt_str:
        return None
    try:
        naive = datetime.datetime.strptime(str(dt_str)[:19], "%Y-%m-%d %H:%M:%S")
        return timezone.make_aware(naive) if timezone.is_naive(naive) else naive
    except ValueError:
        return None


@transaction.atomic
def commit_statement_orders_and_products(
    statement: SettlementStatement,
    preview_data: Dict[str, Any],
    user=None,
) -> Dict[str, int]:
    """
    Persists and reconciles all DS Fees, Completed Sales, Fines, and Auto-Discovered Products
    from a validated Kilimall statement across historical uploads.
    """
    store: Store = statement.store
    sales_by_sn = {row["order_sn"]: row for row in preview_data.get("completed_sales", [])}
    ds_by_sn = {row["order_sn"]: row for row in preview_data.get("ds_fees", [])}
    fines_list = preview_data.get("fines", [])

    # Optional hook: auto-discover products if Milestone 4 `Product` model is present
    product_cogs_map: Dict[str, Decimal] = {}
    try:
        from apps.products.models import Product

        for prod_info in preview_data.get("discovered_products", []):
            gid = prod_info["goods_id"]
            product_obj, _ = Product.objects.update_or_create(
                store=store,
                goods_id=gid,
                defaults={
                    "goods_name": prod_info["goods_name"],
                    "headline_name": prod_info["headline_name"],
                    "latest_selling_price": Decimal(prod_info["unit_price"]),
                    "category_commission_rate": Decimal(prod_info["commission_rate"]),
                },
            )
            if getattr(product_obj, "current_buying_price", None) is not None:
                product_cogs_map[gid] = product_obj.current_buying_price
    except Exception:
        pass

    stats = {
        "same_bill_settled": 0,
        "auto_flipped_from_pending": 0,
        "prior_cycle_settled": 0,
        "new_pending_created": 0,
        "penalties_recorded": 0,
    }

    # STEP 1: Process all Drop-Shipping (DS) Processing Fee rows ('ds processing fee' sheet)
    for order_sn, ds_row in ds_by_sn.items():
        fee_amt = Decimal(str(ds_row["amount"]))
        fee_dt = _to_aware_dt(ds_row.get("fee_date")) or statement.period_end
        pkg_size = ds_row.get("package_size", "Medium")
        shipment_no = ds_row.get("shipment_no", "")

        order, created = Order.objects.get_or_create(
            store=store,
            order_sn=order_sn,
            defaults={
                "workbook_store_alias": ds_row.get("store_name") or store.store_name,
                "settlement_status": Order.STATUS_PENDING,
                "ds_match_type": Order.DS_MATCH_AWAITING_SALE,
                "first_seen_statement": statement,
                "linked_ds_fee": fee_amt,
                "linked_shipment_no": shipment_no,
                "linked_package_size": pkg_size,
                "ds_fee_scan_date": fee_dt,
            },
        )

        if not created:
            order.linked_ds_fee = fee_amt
            order.linked_shipment_no = shipment_no
            order.linked_package_size = pkg_size
            order.ds_fee_scan_date = fee_dt
            if order.settlement_status == Order.STATUS_SETTLED:
                order.ds_match_type = Order.DS_MATCH_SAME_BILL
                order.recalculate_profit()
            order.save()
        else:
            if order_sn not in sales_by_sn:
                stats["new_pending_created"] += 1

        LogisticsFee.objects.update_or_create(
            statement=statement,
            order=order,
            shipment_no=shipment_no,
            defaults={
                "package_size": pkg_size,
                "amount": fee_amt,
                "fee_date": fee_dt,
            },
        )

    # STEP 2: Process all Completed Sales rows ('bill details' sheet)
    for order_sn, sale_row in sales_by_sn.items():
        price = Decimal(str(sale_row["goods_price"]))
        qty = int(sale_row.get("goods_num", 1))
        rate = Decimal(str(sale_row["rate"]))
        comm = Decimal(str(sale_row["commission"]))
        settlement = Decimal(str(sale_row["settlement"]))
        pay_dt = _to_aware_dt(sale_row.get("payment_time"))
        fin_dt = _to_aware_dt(sale_row.get("finished_time"))
        gid = sale_row.get("goods_id", "")

        existing_order = Order.objects.filter(store=store, order_sn=order_sn).first()
        was_previously_pending = bool(
            existing_order
            and existing_order.settlement_status in (Order.STATUS_PENDING, Order.STATUS_OVERDUE)
            and existing_order.first_seen_statement_id != statement.id
        )

        if order_sn in ds_by_sn:
            ds_match_type = Order.DS_MATCH_SAME_BILL
            stats["same_bill_settled"] += 1
        elif was_previously_pending:
            ds_match_type = Order.DS_MATCH_PRIOR_BILL_AUTO_FLIPPED
            stats["auto_flipped_from_pending"] += 1
        else:
            ds_match_type = Order.DS_MATCH_PRIOR_UNTRACKED
            stats["prior_cycle_settled"] += 1

        if existing_order is None:
            order = Order(
                store=store,
                order_sn=order_sn,
                first_seen_statement=statement,
            )
        else:
            order = existing_order

        order.workbook_store_alias = sale_row.get("store_name") or store.store_name
        order.settlement_status = Order.STATUS_SETTLED
        order.settlement_statement = statement
        order.ds_match_type = ds_match_type
        if was_previously_pending:
            order.was_auto_flipped_from_pending = True

        order.goods_id = gid
        order.goods_name = sale_row.get("goods_name", "")
        order.headline_name = sale_row.get("headline_name", "")[:160]
        order.goods_price = price
        order.goods_num = qty
        order.commission_rate = rate
        order.commission_amount = comm
        order.net_settlement = settlement
        order.payment_time = pay_dt
        order.finished_time = fin_dt

        # Check package size anomaly (DS fee >= 35% of selling price)
        if price > 0 and order.linked_ds_fee > 0 and (order.linked_ds_fee / price) >= Decimal("0.35"):
            order.has_package_size_anomaly = True
            LogisticsFee.objects.filter(order=order).update(is_size_anomaly=True)

        # Apply COGS if available for this product
        if gid in product_cogs_map and order.applied_cogs is None:
            order.applied_cogs = product_cogs_map[gid] * qty

        order.recalculate_profit()
        order.save()

    # STEP 3: Process all Fines & Penalties rows ('fine' sheet)
    for fine_row in fines_list:
        order_sn = fine_row["order_sn"]
        fine_amt = Decimal(str(fine_row["fine_amount"]))
        p_code = fine_row.get("penalty_code", Order.PENALTY_OVERDUE_DISPATCH)
        pay_dt = _to_aware_dt(fine_row.get("payment_time"))
        can_dt = _to_aware_dt(fine_row.get("cancel_time"))

        order, created = Order.objects.get_or_create(
            store=store,
            order_sn=order_sn,
            defaults={
                "workbook_store_alias": fine_row.get("store_name") or "Harambee Ⅰ",
                "settlement_status": Order.STATUS_PENALTY_ONLY,
                "penalty_status": p_code,
                "first_seen_statement": statement,
                "payment_time": pay_dt,
                "linked_fine_amount": fine_amt,
                "headline_name": f"Penalized Order ({fine_row.get('reason', 'SLA Fine')})",
            },
        )
        if not created:
            order.penalty_status = p_code
            order.linked_fine_amount = fine_amt
            if not order.payment_time and pay_dt:
                order.payment_time = pay_dt
            order.save()

        PenaltyFine.objects.update_or_create(
            statement=statement,
            order=order,
            defaults={
                "workbook_store_alias": fine_row.get("store_name") or "Harambee Ⅰ",
                "penalty_code": p_code,
                "reason": fine_row.get("reason", "Seller overdue dispatch"),
                "fine_amount": fine_amt,
                "payment_time": pay_dt,
                "cancel_time": can_dt,
                "elapsed_delay_seconds": int(fine_row.get("elapsed_seconds", 0)),
                "elapsed_badge": fine_row.get("elapsed_badge", "—"),
                "remark": fine_row.get("remark", "GS"),
            },
        )
        stats["penalties_recorded"] += 1

    # STEP 4: Refresh aging statuses across all Pending orders for this tenant
    refresh_pending_order_aging(store.tenant)
    return stats


def refresh_pending_order_aging(tenant) -> int:
    """
    Evaluates all `PENDING_SETTLEMENT` and `OVERDUE_PENDING` orders for a tenant against
    `tenant.pending_aging_threshold_days` (default 14 days) and updates their status accordingly.
    """
    threshold_days = getattr(tenant, "pending_aging_threshold_days", 14)
    cutoff_dt = timezone.now() - datetime.timedelta(days=threshold_days)

    newly_overdue = Order.objects.filter(
        store__tenant=tenant,
        settlement_status=Order.STATUS_PENDING,
        ds_fee_scan_date__lt=cutoff_dt,
    ).update(settlement_status=Order.STATUS_OVERDUE)

    # Also revert any Overdue order back to Pending if the user increased their threshold in Settings
    Order.objects.filter(
        store__tenant=tenant,
        settlement_status=Order.STATUS_OVERDUE,
        ds_fee_scan_date__gte=cutoff_dt,
    ).update(settlement_status=Order.STATUS_PENDING)

    return newly_overdue


def build_order_360_timeline(order: Order) -> List[Dict[str, Any]]:
    """
    Constructs the chronological Cross-Statement Audit Timeline displayed inside the
    Order 360° Slide-Over Drawer (`SCR-09`).
    """
    events: List[Dict[str, Any]] = []

    if order.payment_time:
        events.append(
            {
                "timestamp": order.payment_time,
                "step_label": "STEP 1 • CUSTOMER PAYMENT",
                "title": "Customer Order & Payment Recorded",
                "description": (
                    f"Order SN {order.order_sn} paid on Kilimall "
                    f"({f'KES {order.goods_price:,.2f}' if order.goods_price else 'Awaiting sale details'})."
                ),
                "badge": "Payment Verified",
                "tone": "blue",
            }
        )

    for fee in order.logistics_fees.select_related("statement").all():
        events.append(
            {
                "timestamp": fee.fee_date,
                "step_label": "STEP 2 • DROP-SHIPPING HUB SCAN & FEE DEDUCTION",
                "title": f"DS Processing Fee Billed (-KES {fee.amount:,.2f} • {fee.package_size})",
                "description": (
                    f"Shipment {fee.shipment_no} scanned at sorting hub. "
                    f"Deducted in statement '{fee.statement.filename}'."
                ),
                "badge": f"Shipment {fee.shipment_no}",
                "tone": "violet",
            }
        )

    for pen in order.penalties.select_related("statement").all():
        events.append(
            {
                "timestamp": pen.cancel_time or pen.payment_time or order.created_at,
                "step_label": "SLA PENALTY EVENT",
                "title": f"{pen.reason} (-KES {pen.fine_amount:,.2f})",
                "description": (
                    f"Billed under workbook alias '{pen.workbook_store_alias}' after {pen.elapsed_badge} delay "
                    f"in statement '{pen.statement.filename}'."
                ),
                "badge": f"Delay: {pen.elapsed_badge}",
                "tone": "red",
            }
        )

    if order.settlement_status == Order.STATUS_SETTLED and order.finished_time:
        stmt_name = order.settlement_statement.filename if order.settlement_statement else "Settlement Statement"
        auto_note = (
            " (Automatically transitioned from Pending Settlement → Settled!)"
            if order.was_auto_flipped_from_pending
            else ""
        )
        events.append(
            {
                "timestamp": order.finished_time,
                "step_label": "STEP 3 • CUSTOMER DELIVERY & PAYOUT SETTLEMENT",
                "title": f"Order Finished & Sale Settled (+KES {order.net_settlement:,.2f}){auto_note}",
                "description": (
                    f"Gross KES {order.goods_price:,.2f} less Commission KES {order.commission_amount:,.2f} "
                    f"settled in '{stmt_name}'."
                ),
                "badge": "SETTLED",
                "tone": "green",
            }
        )
    elif order.settlement_status in (Order.STATUS_PENDING, Order.STATUS_OVERDUE):
        events.append(
            {
                "timestamp": timezone.now(),
                "step_label": "STEP 3 • AWAITING NEXT WEEKLY SETTLEMENT",
                "title": f"In Transit / Pending Settlement ({order.transit_days_elapsed} Days Elapsed)",
                "description": (
                    "DS processing fee has already been deducted by Kilimall. "
                    "Once the customer receives the package and the order appears in a future 'bill details' sheet, "
                    "KiliLedger Pro will automatically match Order SN and flip status to Settled."
                ),
                "badge": order.get_settlement_status_display(),
                "tone": "amber" if order.settlement_status == Order.STATUS_PENDING else "red",
            }
        )

    return events
