"""
Cross-Week Order Reconciliation & Lifecycle Matching Engine (`services/reconciler.py`).

Responsibilities:
1. Executes `commit_statement_orders_and_products` atomically whenever a Kilimall `.xlsx` statement is committed:
   - Processes all `ds_fees` rows: creates or updates `Order` and `LogisticsFee` records.
   - Processes all `completed_sales` rows (`bill details` sheet): handles positive sales (`goods_num > 0`) and
     negative `goods_num < 0` customer returns.
   - Processes all `fines` rows (`fine` sheet): creates `PenaltyFine` records (`OVERDUE_DISPATCH`, `OUT_OF_STOCK`,
     `DS_LATE_ARRIVAL`).
   - Processes all `other_deductions` rows (`Other Deductions` sheet): creates `OtherDeduction` records
     (`VIOLATION` @ KES 500, `AFTER_SALE_RETURN_SHIPPING` @ KES 200), links them to `Order` and `SKU`,
     accumulates `linked_other_deduction` on the `Order`, and recalculates net order profit.
2. Executes `rollback_statement` atomically whenever a Vendor Owner rolls back an imported statement (`SCR-06`).
3. Evaluates configurable pending aging rules (default `14 days` from `ds_fee_scan_date`).
4. Builds the complete **Order 360° Cross-Statement Audit Timeline** for the slide-over drawer (`SCR-09`).
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Dict, List

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.orders.models import LogisticsFee, Order, OtherDeduction, PenaltyFine
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
    Persists and reconciles all DS Fees, Completed Sales, Customer Returns (`goods_num < 0`),
    Fines, Other Deductions (`violation` & `After-sale return shipping`), and Auto-Discovered Products
    from a validated Kilimall statement across historical uploads.
    """
    store: Store = statement.store
    sales_rows = preview_data.get("completed_sales", [])
    sales_by_sn = {row["order_sn"]: row for row in sales_rows}
    ds_by_sn = {row["order_sn"]: row for row in preview_data.get("ds_fees", [])}
    fines_list = preview_data.get("fines", [])
    other_deductions_list = preview_data.get("other_deductions", [])

    # Auto-discover products if `Product` model is present
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
        "returned_clawbacks_recorded": 0,
        "new_pending_created": 0,
        "penalties_recorded": 0,
        "other_deductions_recorded": 0,
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
            elif order.settlement_status == Order.STATUS_RETURNED:
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

    # STEP 2: Process all rows in 'bill details' (Positive Completed Sales + Negative `goods_num` Returns)
    sorted_sales_rows = sorted(sales_rows, key=lambda r: 1 if int(r.get("goods_num", 1)) < 0 else 0)

    for sale_row in sorted_sales_rows:
        order_sn = sale_row["order_sn"]
        price = Decimal(str(sale_row["goods_price"]))
        qty = int(sale_row.get("goods_num", 1))
        complete_amt = Decimal(
            str(sale_row.get("complete_amount") or (price * Decimal(qty)).quantize(Decimal("0.01")))
        )
        rate = Decimal(str(sale_row["rate"]))
        comm = Decimal(str(sale_row["commission"]))
        settlement = Decimal(str(sale_row["settlement"]))
        pay_dt = _to_aware_dt(sale_row.get("payment_time"))
        fin_dt = _to_aware_dt(sale_row.get("finished_time"))
        gid = sale_row.get("goods_id", "")
        is_return = bool(sale_row.get("is_return") or qty < 0 or complete_amt < Decimal("0.00") or settlement < Decimal("0.00"))

        existing_order = Order.objects.filter(store=store, order_sn=order_sn).first()

        if existing_order is None:
            order = Order(
                store=store,
                order_sn=order_sn,
                first_seen_statement=statement,
            )
        else:
            order = existing_order

        order.workbook_store_alias = sale_row.get("store_name") or store.store_name
        order.settlement_statement = statement
        order.goods_id = gid
        order.goods_name = sale_row.get("goods_name", "")
        order.headline_name = sale_row.get("headline_name", "")[:160]
        order.goods_price = price
        order.goods_num = qty
        order.complete_amount = complete_amt
        order.commission_rate = rate
        order.commission_amount = comm
        order.net_settlement = settlement
        order.payment_time = pay_dt
        order.finished_time = fin_dt

        if gid in product_cogs_map and getattr(order, "unit_buying_price", None) is None:
            order.unit_buying_price = product_cogs_map[gid]

        if is_return:
            order.settlement_status = Order.STATUS_RETURNED
            order.is_return_clawback = True
            order.ds_match_type = Order.DS_MATCH_RETURN_CLAWBACK
            if getattr(order, "return_condition", Order.RETURN_COND_NA) == Order.RETURN_COND_NA:
                order.return_condition = Order.RETURN_COND_RESTOCKED
            stats["returned_clawbacks_recorded"] += 1
        else:
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

            order.settlement_status = Order.STATUS_SETTLED
            order.is_return_clawback = False
            order.return_condition = Order.RETURN_COND_NA
            order.ds_match_type = ds_match_type
            if was_previously_pending:
                order.was_auto_flipped_from_pending = True

            if complete_amt > 0 and order.linked_ds_fee > 0 and (order.linked_ds_fee / complete_amt) >= Decimal("0.35"):
                order.has_package_size_anomaly = True
                LogisticsFee.objects.filter(order=order).update(is_size_anomaly=True)

            if gid in product_cogs_map and order.applied_cogs is None:
                order.applied_cogs = product_cogs_map[gid] * abs(qty)

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
                "workbook_store_alias": fine_row.get("store_name") or store.store_name,
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
                "workbook_store_alias": fine_row.get("store_name") or store.store_name,
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

    # STEP 4: Process all 'Other Deductions' rows ('Other Deductions' sheet — e.g. 'violation' & 'After-sale return shipping')
    other_ded_orders_touched = set()
    for od_row in other_deductions_list:
        order_sn = od_row.get("order_sn") or f"OTHER-DED-{statement.id}"
        sku_str = od_row.get("sku", "")
        ded_type = od_row.get("deduction_type", "Other Deduction")
        ded_code = od_row.get("deduction_code", OtherDeduction.CODE_OTHER)
        amt_dec = abs(Decimal(str(od_row.get("amount", "0.00"))))
        period_str = od_row.get("period", "")
        note_str = od_row.get("note", "")
        alias_str = od_row.get("store_name") or store.store_name

        default_status = (
            Order.STATUS_RETURNED
            if ded_code == OtherDeduction.CODE_RETURN_SHIPPING
            else Order.STATUS_PENALTY_ONLY
        )

        order, created = Order.objects.get_or_create(
            store=store,
            order_sn=order_sn,
            defaults={
                "workbook_store_alias": alias_str,
                "settlement_status": default_status,
                "penalty_status": Order.PENALTY_POST_SALE_VIOLATION,
                "return_condition": Order.RETURN_COND_RESTOCKED if default_status == Order.STATUS_RETURNED else Order.RETURN_COND_NA,
                "first_seen_statement": statement,
                "goods_id": sku_str,
                "headline_name": f"Post-Sale Deduction ({ded_type} • SKU {sku_str})" if sku_str else f"Post-Sale Deduction ({ded_type})",
            },
        )

        if not created:
            if sku_str and not order.goods_id:
                order.goods_id = sku_str
            if order.penalty_status == Order.PENALTY_NONE:
                order.penalty_status = Order.PENALTY_POST_SALE_VIOLATION
            if ded_code == OtherDeduction.CODE_RETURN_SHIPPING and order.settlement_status == Order.STATUS_PENALTY_ONLY:
                order.settlement_status = Order.STATUS_RETURNED
                if order.return_condition == Order.RETURN_COND_NA:
                    order.return_condition = Order.RETURN_COND_RESTOCKED

        if sku_str and sku_str in product_cogs_map and getattr(order, "unit_buying_price", None) is None:
            order.unit_buying_price = product_cogs_map[sku_str]

        OtherDeduction.objects.create(
            statement=statement,
            order=order,
            workbook_store_alias=alias_str,
            sku=sku_str,
            deduction_type=ded_type,
            deduction_code=ded_code,
            amount=amt_dec,
            period=period_str,
            note=note_str,
        )
        other_ded_orders_touched.add(order.id)
        stats["other_deductions_recorded"] += 1

    # Recompute cumulative `linked_other_deduction` and profit on all orders touched by Other Deductions
    if other_ded_orders_touched:
        for order in Order.objects.filter(id__in=other_ded_orders_touched).prefetch_related("other_deductions"):
            order.linked_other_deduction = sum(
                (od.amount for od in order.other_deductions.all()),
                Decimal("0.00"),
            )
            order.recalculate_profit()
            order.save()

    # STEP 5: Refresh aging statuses across all Pending orders for this tenant
    refresh_pending_order_aging(store.tenant)
    return stats


@transaction.atomic
def rollback_statement(
    statement: SettlementStatement,
    purge_unreferenced_unpriced_products: bool = False,
) -> Dict[str, int]:
    """
    Atomically reverses all database side-effects of an imported `SettlementStatement`
    before deleting the statement itself, ensuring zero orphan orders or stale cached fields remain.
    """
    store: Store = statement.store
    tenant = store.tenant
    stmt_id = statement.id

    # 1. Gather all Order IDs associated with this statement across all 5 link paths
    touched_order_ids = set(
        Order.objects.filter(
            Q(first_seen_statement_id=stmt_id)
            | Q(settlement_statement_id=stmt_id)
            | Q(logistics_fees__statement_id=stmt_id)
            | Q(penalties__statement_id=stmt_id)
            | Q(other_deductions__statement_id=stmt_id),
            store=store,
        )
        .values_list("id", flat=True)
        .distinct()
    )

    # 2. Delete child LogisticsFee, PenaltyFine, and OtherDeduction rows belonging to this statement
    deleted_fees_count, _ = LogisticsFee.objects.filter(statement_id=stmt_id).delete()
    deleted_fines_count, _ = PenaltyFine.objects.filter(statement_id=stmt_id).delete()
    deleted_other_ded_count, _ = OtherDeduction.objects.filter(statement_id=stmt_id).delete()

    orders_deleted = 0
    orders_reverted_to_pending = 0
    orders_recalculated = 0

    # 3. Inspect and either delete or revert/recalculate every touched Order
    touched_orders = Order.objects.filter(id__in=touched_order_ids).prefetch_related(
        "logistics_fees__statement",
        "penalties__statement",
        "other_deductions__statement",
    )

    for order in touched_orders:
        remaining_fees = [f for f in order.logistics_fees.all() if f.statement_id != stmt_id]
        remaining_fines = [p for p in order.penalties.all() if p.statement_id != stmt_id]
        remaining_other_deds = [
            od for od in getattr(order, "other_deductions", order.logistics_fees).all()
            if getattr(od, "statement_id", stmt_id) != stmt_id
        ] if hasattr(order, "other_deductions") else []

        other_first_seen = (
            order.first_seen_statement_id
            if (order.first_seen_statement_id and order.first_seen_statement_id != stmt_id)
            else None
        )
        other_settlement = (
            order.settlement_statement_id
            if (order.settlement_statement_id and order.settlement_statement_id != stmt_id)
            else None
        )

        # If this order has zero remaining footprint in any other statement, delete it completely
        if not remaining_fees and not remaining_fines and not remaining_other_deds and not other_first_seen and not other_settlement:
            order.delete()
            orders_deleted += 1
            continue

        # Re-anchor first_seen_statement if it pointed to the rolled-back statement
        if order.first_seen_statement_id == stmt_id:
            earliest_stmt_id = None
            if remaining_fees:
                earliest_stmt_id = sorted(remaining_fees, key=lambda x: x.fee_date)[0].statement_id
            elif remaining_fines:
                earliest_stmt_id = remaining_fines[0].statement_id
            elif remaining_other_deds:
                earliest_stmt_id = remaining_other_deds[0].statement_id
            elif other_settlement:
                earliest_stmt_id = other_settlement
            order.first_seen_statement_id = earliest_stmt_id

        # Recalculate cached DS fee attributes from remaining LogisticsFee rows
        if remaining_fees:
            latest_fee = sorted(remaining_fees, key=lambda x: x.fee_date)[-1]
            order.linked_ds_fee = sum((f.amount for f in remaining_fees), Decimal("0.00"))
            order.linked_shipment_no = latest_fee.shipment_no
            order.linked_package_size = latest_fee.package_size
            order.ds_fee_scan_date = latest_fee.fee_date
        else:
            order.linked_ds_fee = Decimal("0.00")
            order.linked_shipment_no = ""
            order.linked_package_size = ""
            order.ds_fee_scan_date = None

        # Recalculate cached SLA Penalty & Other Deduction attributes
        if remaining_fines:
            latest_fine = remaining_fines[0]
            order.linked_fine_amount = sum((p.fine_amount for p in remaining_fines), Decimal("0.00"))
            order.penalty_status = latest_fine.penalty_code
        elif remaining_other_deds:
            order.linked_fine_amount = Decimal("0.00")
            order.penalty_status = Order.PENALTY_POST_SALE_VIOLATION
        else:
            order.linked_fine_amount = Decimal("0.00")
            order.penalty_status = Order.PENALTY_NONE

        order.linked_other_deduction = sum((od.amount for od in remaining_other_deds), Decimal("0.00"))

        # If the completed sale or return clawback was recorded in the rolled-back statement, revert it
        if order.settlement_statement_id == stmt_id:
            order.settlement_statement = None
            order.was_auto_flipped_from_pending = False
            order.is_return_clawback = False
            order.return_condition = Order.RETURN_COND_NA
            order.goods_price = None
            order.goods_num = 1
            order.complete_amount = None
            order.commission_rate = None
            order.commission_amount = None
            order.net_settlement = None
            order.finished_time = None
            order.applied_cogs = None
            order.net_order_profit = None
            order.has_package_size_anomaly = False

            if remaining_fees:
                order.settlement_status = Order.STATUS_PENDING
                order.ds_match_type = Order.DS_MATCH_AWAITING_SALE
            elif remaining_fines or remaining_other_deds:
                order.settlement_status = Order.STATUS_PENALTY_ONLY
                order.ds_match_type = Order.DS_MATCH_NONE
            else:
                order.delete()
                orders_deleted += 1
                continue

            orders_reverted_to_pending += 1
        else:
            if order.settlement_status in (Order.STATUS_SETTLED, Order.STATUS_RETURNED):
                if not remaining_fees and order.settlement_status == Order.STATUS_SETTLED:
                    order.ds_match_type = Order.DS_MATCH_PRIOR_UNTRACKED
                    order.has_package_size_anomaly = False
                elif order.complete_amount and order.complete_amount > 0:
                    order.has_package_size_anomaly = (order.linked_ds_fee / order.complete_amount) >= Decimal("0.35")
                order.recalculate_profit()
            orders_recalculated += 1

        order.save()

    # 4. Clean up VendorNotification entries created for this statement
    notifications_removed = 0
    try:
        from apps.accounts.models import VendorNotification

        notifications_removed, _ = VendorNotification.objects.filter(
            tenant=tenant,
            action_url__contains=str(stmt_id),
        ).delete()
    except Exception:
        pass

    # 5. Optional cleanup of unpriced auto-discovered products with zero remaining orders
    products_removed = 0
    if purge_unreferenced_unpriced_products:
        try:
            from apps.products.models import Product

            active_goods_ids = set(
                Order.objects.filter(store=store)
                .exclude(goods_id="")
                .values_list("goods_id", flat=True)
                .distinct()
            )
            products_removed, _ = (
                Product.objects.filter(
                    store=store,
                    current_buying_price__isnull=True,
                )
                .exclude(goods_id__in=active_goods_ids)
                .delete()
            )
        except Exception:
            pass

    # 6. Delete the SettlementStatement record itself and refresh pending aging
    statement.delete()
    refresh_pending_order_aging(tenant)

    return {
        "orders_deleted": orders_deleted,
        "orders_reverted_to_pending": orders_reverted_to_pending,
        "orders_recalculated": orders_recalculated,
        "logistics_fees_deleted": deleted_fees_count,
        "penalties_deleted": deleted_fines_count,
        "other_deductions_deleted": deleted_other_ded_count,
        "notifications_removed": notifications_removed,
        "unpriced_products_removed": products_removed,
    }


def refresh_pending_order_aging(tenant) -> int:
    threshold_days = getattr(tenant, "pending_aging_threshold_days", 14)
    cutoff_dt = timezone.now() - datetime.timedelta(days=threshold_days)

    newly_overdue = Order.objects.filter(
        store__tenant=tenant,
        settlement_status=Order.STATUS_PENDING,
        ds_fee_scan_date__lt=cutoff_dt,
    ).update(settlement_status=Order.STATUS_OVERDUE)

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
                    f"({f'Unit Price KES {order.goods_price:,.2f} × {abs(order.goods_num)}' if order.goods_price else 'Awaiting sale details'})."
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
                "step_label": "SLA PENALTY EVENT ('fine' Sheet)",
                "title": f"{pen.reason} (-KES {pen.fine_amount:,.2f})",
                "description": (
                    f"Billed under workbook alias '{pen.workbook_store_alias}' after {pen.elapsed_badge} delay "
                    f"in statement '{pen.statement.filename}'."
                ),
                "badge": f"Delay: {pen.elapsed_badge}",
                "tone": "red",
            }
        )

    if hasattr(order, "other_deductions"):
        for od in order.other_deductions.select_related("statement").all():
            events.append(
                {
                    "timestamp": od.created_at,
                    "step_label": "OTHER DEDUCTION EVENT ('Other Deductions' Sheet)",
                    "title": f"{od.deduction_type} (-KES {od.amount:,.2f})",
                    "description": (
                        f"Post-sale deduction on SKU {od.sku or order.goods_id or '—'} "
                        f"for period '{od.period or 'Statement Cycle'}' in '{od.statement.filename}'."
                    ),
                    "badge": od.get_deduction_code_display(),
                    "tone": "red",
                }
            )

    if order.settlement_status == Order.STATUS_RETURNED or (order.goods_num is not None and order.goods_num < 0):
        stmt_name = order.settlement_statement.filename if order.settlement_statement else "Settlement Statement"
        cond_label = (
            "Resellable / Restocked (COGS Recovered)"
            if order.return_condition == Order.RETURN_COND_RESTOCKED
            else "Damaged / Lost (Wholesale COGS Written Off)"
        )
        events.append(
            {
                "timestamp": order.finished_time or timezone.now(),
                "step_label": "STEP 3 • CUSTOMER RETURN / AFTER-SALE STATUS",
                "title": (
                    f"Returned Item Clawback ({order.net_settlement or Decimal('0.00'):,.2f} KES • Qty {order.goods_num})"
                    if order.is_return_clawback
                    else f"After-Sale Returned Order (-KES {order.linked_other_deduction:,.2f} Other Deductions)"
                ),
                "description": (
                    f"Inventory Condition: {cond_label}. "
                    + (
                        f"Kilimall reversed sale ({order.effective_gross_amount or Decimal('0.00'):,.2f} KES) and refunded "
                        f"+KES {order.commission_amount or Decimal('0.00'):,.2f} commission in '{stmt_name}'."
                        if order.is_return_clawback
                        else "Tracked via Other Deductions / Return Shipping."
                    )
                ),
                "badge": "RETURNED / CLAWBACK",
                "tone": "red",
            }
        )
    elif order.settlement_status == Order.STATUS_SETTLED and order.finished_time:
        stmt_name = order.settlement_statement.filename if order.settlement_statement else "Settlement Statement"
        auto_note = (
            " (Automatically transitioned from Pending Settlement → Settled!)"
            if order.was_auto_flipped_from_pending
            else ""
        )
        gross_val = order.effective_gross_amount or order.goods_price or Decimal("0.00")
        events.append(
            {
                "timestamp": order.finished_time,
                "step_label": "STEP 3 • CUSTOMER DELIVERY & PAYOUT SETTLEMENT",
                "title": f"Order Finished & Sale Settled (+KES {order.net_settlement:,.2f}){auto_note}",
                "description": (
                    f"Gross KES {gross_val:,.2f} (Qty {order.goods_num}) less Commission KES {abs(order.commission_amount or Decimal('0.00')):,.2f} "
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
