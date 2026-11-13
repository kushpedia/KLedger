"""
Decoupled 9-Sheet Kilimall Settlement Workbook Parser & Structural Validator (`services/parser.py`).

Responsibilities:
1. Parses any Kilimall `.xlsx` settlement workbook in memory using `openpyxl(data_only=True)`.
2. Inspects all 9 expected worksheets (`bill`, `bill details`, `operation fee `, `storage fee`,
   `ds processing fee`, `fine`, `Other Deductions`, `Billing Appeal`, `compensation`).
3. Maps known Kilimall column header typos (`Total Valume` -> `total_volume`, `finnshed_time` -> `finished_time`,
   `amout` -> `amount`, `Amount（ksh）` -> `amount`) and records schema diagnostics for `SCR-16`.
4. Sanitizes leading commas on order numbers (`",610244462479107"` -> `"610244462479107"`).
5. Explicitly detects Customer Return / Payout Clawback rows in `bill details` where `goods_num < 0`.
6. Converts Excel serial duration timestamps (`1900-01-13 00:08:45`) AND fractional float days
   (`6.429745` -> `6d 10h 18m`) in the `fine` worksheet into exact elapsed seconds and delay badges,
   and classifies `DS late arrival` (`-200 KES`), `Seller overdue dispatch` (`-300 KES`), and `Seller confirmed out of stock` (`-300 KES`).
7. Parses all rows in Sheet 7 (`Other Deductions`), mapping full-width `Amount（ksh）` headers, cleaning
   leading commas on `Order SN`, and classifying `violation` (`-500 KES`) vs. `After-sale return shipping` (`-200 KES`).
8. Verifies the Two-Stage Kilimall Payout Equation to the cent.
"""
from __future__ import annotations

import datetime
import hashlib
import io
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO, Dict, List, Optional, Set, Tuple

import openpyxl


EXPECTED_SHEETS = [
    "bill",
    "bill details",
    "operation fee ",
    "storage fee",
    "ds processing fee",
    "fine",
    "Other Deductions",
    "Billing Appeal",
    "compensation",
]


def clean_order_sn(raw_val: Any) -> str:
    """Strips leading commas, quotes, and whitespace from Kilimall order numbers."""
    if raw_val is None:
        return ""
    cleaned = str(raw_val).strip().strip('"').strip("'").lstrip(",").strip().strip('"').strip("'")
    if cleaned.endswith(".0"):
        cleaned = cleaned[:-2]
    return cleaned


def clean_identifier(raw_val: Any) -> str:
    """Normalizes Store IDs, Goods IDs, and Shipment Tracking numbers."""
    if raw_val is None:
        return ""
    s = str(raw_val).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def to_decimal(raw_val: Any, default: str = "0.00") -> Decimal:
    """Converts cell values safely into 2-decimal Decimal objects."""
    if raw_val is None or str(raw_val).strip() == "" or str(raw_val).lower() == "nan":
        return Decimal(default)
    try:
        return Decimal(str(raw_val).strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def to_rate_decimal(raw_val: Any) -> Decimal:
    """Converts commission rate (e.g. 0.16) into a 4-decimal Decimal."""
    if raw_val is None or str(raw_val).strip() == "":
        return Decimal("0.0000")
    try:
        return Decimal(str(raw_val).strip()).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        return Decimal("0.0000")


def parse_timestamp_str(raw_val: Any) -> Optional[str]:
    """Normalizes Excel datetime or string timestamps to ISO 'YYYY-MM-DD HH:MM:SS' strings."""
    if raw_val is None or str(raw_val).strip() == "":
        return None
    if isinstance(raw_val, datetime.datetime):
        return raw_val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(raw_val, datetime.date):
        return raw_val.strftime("%Y-%m-%d 00:00:00")
    s = str(raw_val).strip()
    return s if s and s.lower() != "nan" else None


def parse_excel_spend_time(
    raw_val: Any,
    payment_time_str: Optional[str] = None,
    cancel_time_str: Optional[str] = None,
) -> Tuple[int, str]:
    """
    Converts Kilimall's Excel duration value in the `fine` tab (`spend_time`):
    - Supports `datetime(1900, 1, 13, 0, 8, 45)` -> `(1123725, '13d 00h 08m')`
    - Supports float/int/numeric-string fractional days (e.g. `6.42974537` -> `(555530, '6d 10h 18m')`)
    - Supports `datetime.timedelta`
    - Falls back to difference between `cancel_time_str` (or `transfer_warehouse_time`) and `payment_time_str`.
    """
    if isinstance(raw_val, datetime.datetime) and raw_val.year == 1900:
        days = raw_val.day
        hours = raw_val.hour
        minutes = raw_val.minute
        seconds = raw_val.second
        total_seconds = (days * 86400) + (hours * 3600) + (minutes * 60) + seconds
        return total_seconds, f"{days}d {hours:02d}h {minutes:02d}m"

    if isinstance(raw_val, datetime.timedelta):
        total_seconds = int(raw_val.total_seconds())
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        return total_seconds, f"{days}d {hours:02d}h {minutes:02d}m"

    # Support float/int fractional days (e.g. 6.42974537036935 in bill (10).xlsx)
    if isinstance(raw_val, (int, float, Decimal)) and not isinstance(raw_val, bool):
        f_days = float(raw_val)
        if f_days > 0:
            total_seconds = int(round(f_days * 86400))
            days = total_seconds // 86400
            hours = (total_seconds % 86400) // 3600
            minutes = (total_seconds % 3600) // 60
            return total_seconds, f"{days}d {hours:02d}h {minutes:02d}m"

    if isinstance(raw_val, str):
        s_val = raw_val.strip()
        if s_val.startswith("1900-01-"):
            try:
                dt = datetime.datetime.strptime(s_val[:19], "%Y-%m-%d %H:%M:%S")
                days = dt.day
                total_seconds = (days * 86400) + (dt.hour * 3600) + (dt.minute * 60) + dt.second
                return total_seconds, f"{days}d {dt.hour:02d}h {dt.minute:02d}m"
            except ValueError:
                pass
        else:
            try:
                f_days = float(s_val)
                if f_days > 0:
                    total_seconds = int(round(f_days * 86400))
                    days = total_seconds // 86400
                    hours = (total_seconds % 86400) // 3600
                    minutes = (total_seconds % 3600) // 60
                    return total_seconds, f"{days}d {hours:02d}h {minutes:02d}m"
            except ValueError:
                pass

    if payment_time_str and cancel_time_str:
        try:
            p_dt = datetime.datetime.strptime(payment_time_str[:19], "%Y-%m-%d %H:%M:%S")
            c_dt = datetime.datetime.strptime(cancel_time_str[:19], "%Y-%m-%d %H:%M:%S")
            diff = max(0, int((c_dt - p_dt).total_seconds()))
            days = diff // 86400
            hours = (diff % 86400) // 3600
            minutes = (diff % 3600) // 60
            return diff, f"{days}d {hours:02d}h {minutes:02d}m"
        except ValueError:
            pass

    return 0, "—"


@dataclass
class SheetDiagnostic:
    sheet_name: str
    found: bool
    row_count: int
    status: str  # 'VALID' | 'EMPTY_SKIPPED' | 'MISSING' | 'ERROR'
    notes: str


@dataclass
class ParsedWorkbookResult:
    filename: str
    file_checksum: str
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    schema_diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    sheet_checklist: List[Dict[str, Any]] = field(default_factory=list)

    # Store Identity & Aliases
    kilimall_store_id: str = ""
    primary_store_name: str = ""
    discovered_aliases: List[Dict[str, str]] = field(default_factory=list)

    # Period Window
    period_start: Optional[str] = None
    period_end: Optional[str] = None

    # Summary Financials from 'bill' sheet (Stored as strings for JSON session serialization)
    summary: Dict[str, str] = field(default_factory=dict)
    math_reconciled: bool = False

    # Sanitized Row Payloads
    completed_sales: List[Dict[str, Any]] = field(default_factory=list)
    returned_sales: List[Dict[str, Any]] = field(default_factory=list)
    ds_fees: List[Dict[str, Any]] = field(default_factory=list)
    fines: List[Dict[str, Any]] = field(default_factory=list)
    other_deductions: List[Dict[str, Any]] = field(default_factory=list)
    discovered_products: List[Dict[str, Any]] = field(default_factory=list)

    # Pre-Commit Reconciliation Impact Metrics
    reconciliation_impact: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class KilimallWorkbookParser:
    """
    Production-grade parser for Kilimall 9-sheet weekly settlement `.xlsx` workbooks.
    """

    def parse_file(
        self,
        file_obj: BinaryIO | bytes | str,
        filename: str = "bill.xlsx",
        existing_pending_order_sns: Optional[Set[str]] = None,
        existing_product_goods_ids: Optional[Set[str]] = None,
    ) -> ParsedWorkbookResult:
        if isinstance(file_obj, (str, bytes)):
            if isinstance(file_obj, str):
                with open(file_obj, "rb") as f:
                    raw_bytes = f.read()
            else:
                raw_bytes = file_obj
        else:
            file_obj.seek(0)
            raw_bytes = file_obj.read()

        checksum = hashlib.sha256(raw_bytes).hexdigest()
        result = ParsedWorkbookResult(filename=filename, file_checksum=checksum, is_valid=True)

        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
        except Exception as exc:
            result.is_valid = False
            result.errors.append(f"Unable to read Excel workbook '{filename}': {exc}")
            return result

        actual_sheets = {s.strip().lower(): s for s in wb.sheetnames}
        aliases_map: Dict[str, str] = {}

        # 1. Inspect all 9 expected sheets and build diagnostic checklist
        for expected in EXPECTED_SHEETS:
            norm_key = expected.strip().lower()
            if norm_key in actual_sheets:
                real_name = actual_sheets[norm_key]
                ws = wb[real_name]
                data_rows = max(0, ws.max_row - 1)
                if data_rows == 1:
                    row2_vals = [ws.cell(2, c).value for c in range(1, ws.max_column + 1)]
                    if not any(v is not None and str(v).strip() != "" for v in row2_vals):
                        data_rows = 0

                status = "VALID" if data_rows > 0 else "EMPTY_SKIPPED"
                note = f"{data_rows} data row(s) extracted" if data_rows > 0 else "0 rows (Drop Shipping store — safely skipped)"
                result.sheet_checklist.append(
                    asdict(SheetDiagnostic(sheet_name=real_name, found=True, row_count=data_rows, status=status, notes=note))
                )
            else:
                is_critical = norm_key in ("bill", "bill details")
                status = "ERROR" if is_critical else "MISSING"
                if is_critical:
                    result.is_valid = False
                    result.errors.append(f"Required worksheet '{expected}' is missing from this workbook.")
                result.sheet_checklist.append(
                    asdict(
                        SheetDiagnostic(
                            sheet_name=expected,
                            found=False,
                            row_count=0,
                            status=status,
                            notes="Missing required worksheet" if is_critical else "Optional sheet not present",
                        )
                    )
                )

        if not result.is_valid:
            return result

        # 2. Parse Sheet 1: 'bill' (Master Settlement Summary)
        ws_bill = wb[actual_sheets["bill"]]
        bill_headers = {str(ws_bill.cell(1, c).value or "").strip(): c for c in range(1, ws_bill.max_column + 1)}

        if "Total Valume" in bill_headers:
            result.schema_diagnostics.append(
                {
                    "sheet_name": "bill",
                    "issue_type": "HEADER_ALIAS_MAPPED",
                    "diagnostic_detail": "Mapped Kilimall header 'Total Valume' -> 'total_volume'",
                    "auto_resolved": True,
                }
            )

        def get_bill_cell(col_candidates: List[str]) -> Any:
            for cand in col_candidates:
                for h_name, c_idx in bill_headers.items():
                    if h_name.lower() == cand.lower():
                        return ws_bill.cell(2, c_idx).value
            return None

        store_id = clean_identifier(get_bill_cell(["Store ID", "store_id"]))
        store_name = str(get_bill_cell(["Store Name", "store_name"]) or "").strip()
        result.kilimall_store_id = store_id
        result.primary_store_name = store_name
        if store_name:
            aliases_map[store_name] = "bill"

        total_volume = to_decimal(get_bill_cell(["Total Valume", "Total Volume"]))
        total_commission = to_decimal(get_bill_cell(["Total Commission", "Commission"]))
        quality_fee = to_decimal(get_bill_cell(["Ds quality inspection fee", "Quality inspection fee"]))
        payable_1 = to_decimal(get_bill_cell(["Settlement Payable 1"]))
        fines_total = to_decimal(get_bill_cell(["Fine", "Fines"]))
        warehouse_op_fee = to_decimal(get_bill_cell(["Warehouse operation fee"]))
        warehouse_storage_fee = to_decimal(get_bill_cell(["Warehouse storage fee"]))
        ds_fee_total = to_decimal(get_bill_cell(["DS processing fee", "Ds processing fee"]))
        lite_shipping_fee = to_decimal(get_bill_cell(["lite shipping fee"]))
        other_deductions_total = to_decimal(get_bill_cell(["Other Deductions"]))
        billing_appeal_total = to_decimal(get_bill_cell(["Billing Appeal"]))
        compensations_total = to_decimal(get_bill_cell(["Compensations", "Compensation"]))
        payable_2 = to_decimal(get_bill_cell(["Settlement Payable 2"]))
        final_settlement = to_decimal(get_bill_cell(["Final settlement amount", "Final Settlement Payable"]))

        # Verify Two-Stage Payout Equation
        calc_payable_1 = (total_volume + total_commission + quality_fee).quantize(Decimal("0.01"))
        calc_final = (
            calc_payable_1
            + fines_total
            + warehouse_op_fee
            + warehouse_storage_fee
            + ds_fee_total
            + lite_shipping_fee
            + other_deductions_total
            + billing_appeal_total
            + compensations_total
        ).quantize(Decimal("0.01"))

        result.math_reconciled = (calc_payable_1 == payable_1) and (calc_final == final_settlement)
        if not result.math_reconciled:
            result.warnings.append(
                f"Summary equation variance detected: Expected Final {calc_final}, Sheet reported {final_settlement}."
            )

        effective_comm_pct = (
            (abs(total_commission) / total_volume * Decimal("100")).quantize(Decimal("0.01"))
            if total_volume > 0
            else Decimal("0.00")
        )
        retention_pct = (
            (final_settlement / total_volume * Decimal("100")).quantize(Decimal("0.01"))
            if total_volume > 0
            else Decimal("0.00")
        )

        # 3. Parse Sheet 2: 'bill details' (Completed Order Sales AND Negative `goods_num` Customer Returns)
        ws_details = wb[actual_sheets["bill details"]]
        det_headers = {str(ws_details.cell(1, c).value or "").strip().lower(): c for c in range(1, ws_details.max_column + 1)}
        if "finnshed_time" in det_headers:
            result.schema_diagnostics.append(
                {
                    "sheet_name": "bill details",
                    "issue_type": "HEADER_ALIAS_MAPPED",
                    "diagnostic_detail": "Mapped Kilimall header 'finnshed_time' -> 'finished_time'",
                    "auto_resolved": True,
                }
            )

        products_by_id: Dict[str, Dict[str, Any]] = {}
        all_timestamps: List[str] = []

        gross_positive_sales_sum = Decimal("0.00")
        returned_sales_sum = Decimal("0.00")
        positive_commission_sum = Decimal("0.00")
        refunded_commission_sum = Decimal("0.00")
        returned_net_clawback_sum = Decimal("0.00")
        positive_units_sold = 0
        returned_units_count = 0

        for r in range(2, ws_details.max_row + 1):
            raw_sn = ws_details.cell(r, det_headers.get("order_sn", 3)).value
            order_sn = clean_order_sn(raw_sn)
            if not order_sn:
                continue

            row_store_name = str(ws_details.cell(r, det_headers.get("store_name", 2)).value or "").strip()
            if row_store_name and row_store_name not in aliases_map:
                aliases_map[row_store_name] = "bill details"

            payment_time = parse_timestamp_str(ws_details.cell(r, det_headers.get("payment_time", 4)).value)
            finished_time = parse_timestamp_str(
                ws_details.cell(r, det_headers.get("finnshed_time") or det_headers.get("finished_time", 5)).value
            )
            if finished_time:
                all_timestamps.append(finished_time)

            goods_id = clean_identifier(ws_details.cell(r, det_headers.get("goods_id", 6)).value)
            full_goods_name = str(ws_details.cell(r, det_headers.get("goods_name", 7)).value or "").strip()
            headline_name = full_goods_name.splitlines()[0].strip() if full_goods_name else f"Product {goods_id}"

            goods_price = to_decimal(ws_details.cell(r, det_headers.get("goods_price", 8)).value)
            goods_num = int(to_decimal(ws_details.cell(r, det_headers.get("goods_num", 9)).value, "1"))
            rate = to_rate_decimal(ws_details.cell(r, det_headers.get("rate", 10)).value)

            raw_complete_amt = ws_details.cell(
                r,
                det_headers.get("complete amount")
                or det_headers.get("complete_amount")
                or 11,
            ).value
            if raw_complete_amt is not None and str(raw_complete_amt).strip() != "":
                complete_amount = to_decimal(raw_complete_amt)
            else:
                complete_amount = (goods_price * Decimal(goods_num)).quantize(Decimal("0.01"))

            commission = to_decimal(ws_details.cell(r, det_headers.get("commission", 12)).value)
            settlement = to_decimal(ws_details.cell(r, det_headers.get("settlement", 14)).value)

            is_return = goods_num < 0 or complete_amount < Decimal("0.00") or settlement < Decimal("0.00")

            if is_return:
                returned_sales_sum += complete_amount
                refunded_commission_sum += commission
                returned_net_clawback_sum += settlement
                returned_units_count += abs(goods_num)
            else:
                gross_positive_sales_sum += complete_amount
                positive_commission_sum += commission
                positive_units_sold += goods_num

            row_dict = {
                "order_sn": order_sn,
                "store_name": row_store_name or store_name,
                "payment_time": payment_time,
                "finished_time": finished_time,
                "goods_id": goods_id,
                "goods_name": full_goods_name,
                "headline_name": headline_name[:95],
                "goods_price": str(goods_price),
                "goods_num": goods_num,
                "complete_amount": str(complete_amount),
                "rate": str(rate),
                "rate_pct": str((rate * Decimal("100")).quantize(Decimal("0.1"))),
                "commission": str(commission),
                "settlement": str(settlement),
                "is_return": is_return,
            }

            result.completed_sales.append(row_dict)
            if is_return:
                result.returned_sales.append(row_dict)

            if goods_id:
                if goods_id not in products_by_id:
                    products_by_id[goods_id] = {
                        "goods_id": goods_id,
                        "goods_name": full_goods_name,
                        "headline_name": headline_name[:95],
                        "unit_price": str(goods_price),
                        "commission_rate": str(rate),
                        "commission_rate_pct": str((rate * Decimal("100")).quantize(Decimal("0.1"))),
                        "units_in_statement": goods_num if not is_return else 0,
                        "returned_units_in_statement": abs(goods_num) if is_return else 0,
                        "is_new": existing_product_goods_ids is None or goods_id not in existing_product_goods_ids,
                    }
                else:
                    if is_return:
                        products_by_id[goods_id]["returned_units_in_statement"] += abs(goods_num)
                    else:
                        products_by_id[goods_id]["units_in_statement"] += goods_num

        result.discovered_products = list(products_by_id.values())

        result.summary = {
            "total_volume": str(total_volume),
            "gross_positive_sales": str(gross_positive_sales_sum),
            "returned_sales_amount": str(returned_sales_sum),
            "positive_commission": str(positive_commission_sum),
            "refunded_commission": str(refunded_commission_sum),
            "returned_net_clawback": str(returned_net_clawback_sum),
            "positive_units_sold": str(positive_units_sold),
            "returned_units_count": str(returned_units_count),
            "total_commission": str(total_commission),
            "quality_inspection_fee": str(quality_fee),
            "settlement_payable_1": str(payable_1),
            "fines": str(fines_total),
            "warehouse_operation_fee": str(warehouse_op_fee),
            "warehouse_storage_fee": str(warehouse_storage_fee),
            "ds_processing_fee": str(ds_fee_total),
            "lite_shipping_fee": str(lite_shipping_fee),
            "other_deductions": str(other_deductions_total),
            "billing_appeal": str(billing_appeal_total),
            "compensations": str(compensations_total),
            "settlement_payable_2": str(payable_2),
            "final_settlement_amount": str(final_settlement),
            "effective_commission_pct": str(effective_comm_pct),
            "payout_retention_pct": str(retention_pct),
        }

        # 4. Parse Sheet 5: 'ds processing fee' (Drop Shipping Package Handling Fees)
        if "ds processing fee" in actual_sheets:
            ws_ds = wb[actual_sheets["ds processing fee"]]
            ds_headers = {str(ws_ds.cell(1, c).value or "").strip().lower(): c for c in range(1, ws_ds.max_column + 1)}
            if "amout" in ds_headers:
                result.schema_diagnostics.append(
                    {
                        "sheet_name": "ds processing fee",
                        "issue_type": "HEADER_ALIAS_MAPPED",
                        "diagnostic_detail": "Mapped Kilimall header typo 'amout' -> 'amount'",
                        "auto_resolved": True,
                    }
                )

            for r in range(2, ws_ds.max_row + 1):
                raw_sn = ws_ds.cell(r, ds_headers.get("order_no") or ds_headers.get("order_sn", 4)).value
                order_sn = clean_order_sn(raw_sn)
                if not order_sn:
                    continue

                fee_date = parse_timestamp_str(ws_ds.cell(r, ds_headers.get("date", 1)).value)
                if fee_date:
                    all_timestamps.append(fee_date)

                row_store_name = str(ws_ds.cell(r, ds_headers.get("store name", 3)).value or "").strip()
                if row_store_name and row_store_name not in aliases_map:
                    aliases_map[row_store_name] = "ds processing fee"

                shipment_no = clean_identifier(ws_ds.cell(r, ds_headers.get("shipment_no", 5)).value)
                package_size = str(ws_ds.cell(r, ds_headers.get("package_size", 6)).value or "Medium").strip()
                amount = to_decimal(
                    ws_ds.cell(r, ds_headers.get("amout") or ds_headers.get("amount", 7)).value
                )

                result.ds_fees.append(
                    {
                        "order_sn": order_sn,
                        "fee_date": fee_date,
                        "store_name": row_store_name or store_name,
                        "shipment_no": shipment_no,
                        "package_size": package_size,
                        "amount": str(amount),
                    }
                )

        # 5. Parse Sheet 6: 'fine' (Overdue Dispatch, Out-of-Stock & DS Late Arrival Penalties)
        if "fine" in actual_sheets:
            ws_fine = wb[actual_sheets["fine"]]
            fine_headers = {str(ws_fine.cell(1, c).value or "").strip().lower(): c for c in range(1, ws_fine.max_column + 1)}

            for r in range(2, ws_fine.max_row + 1):
                raw_sn = ws_fine.cell(r, fine_headers.get("order_sn", 1)).value
                order_sn = clean_order_sn(raw_sn)
                if not order_sn:
                    continue

                row_store_name = str(ws_fine.cell(r, fine_headers.get("store_name", 3)).value or "").strip()
                if row_store_name and row_store_name not in aliases_map:
                    aliases_map[row_store_name] = "fine"

                payment_time = parse_timestamp_str(ws_fine.cell(r, fine_headers.get("payment_time", 5)).value)
                cancel_time = parse_timestamp_str(ws_fine.cell(r, fine_headers.get("cancel_time", 7)).value)
                transfer_wh_time = parse_timestamp_str(ws_fine.cell(r, fine_headers.get("transfer_warehouse_time", 8)).value)
                raw_spend = ws_fine.cell(r, fine_headers.get("spend_time", 9)).value
                elapsed_secs, elapsed_badge = parse_excel_spend_time(
                    raw_spend,
                    payment_time,
                    cancel_time or transfer_wh_time,
                )

                fine_amt = to_decimal(ws_fine.cell(r, fine_headers.get("fine(ksh)", 10)).value, "300.00")
                notes = str(ws_fine.cell(r, fine_headers.get("notes", 11)).value or "").strip()
                remark = str(ws_fine.cell(r, fine_headers.get("remark", 12)).value or "").strip()

                notes_low = notes.lower()
                if "out of stock" in notes_low:
                    penalty_code = "OUT_OF_STOCK"
                elif "late arrival" in notes_low:
                    penalty_code = "DS_LATE_ARRIVAL"
                else:
                    penalty_code = "OVERDUE_DISPATCH"

                result.fines.append(
                    {
                        "order_sn": order_sn,
                        "store_name": row_store_name or store_name,
                        "payment_time": payment_time,
                        "cancel_time": cancel_time or transfer_wh_time,
                        "elapsed_seconds": elapsed_secs,
                        "elapsed_badge": elapsed_badge,
                        "fine_amount": str(fine_amt),
                        "reason": notes or "Seller penalty deduction",
                        "penalty_code": penalty_code,
                        "remark": remark,
                    }
                )

        # 6. Parse Sheet 7: 'Other Deductions' (Violations & After-Sale Return Shipping Fees)
        violation_deductions_sum = Decimal("0.00")
        return_shipping_deductions_sum = Decimal("0.00")
        other_misc_deductions_sum = Decimal("0.00")

        if "other deductions" in actual_sheets:
            ws_other = wb[actual_sheets["other deductions"]]
            other_headers = {str(ws_other.cell(1, c).value or "").strip().lower(): c for c in range(1, ws_other.max_column + 1)}

            if "amount（ksh）" in other_headers:
                result.schema_diagnostics.append(
                    {
                        "sheet_name": "Other Deductions",
                        "issue_type": "HEADER_ALIAS_MAPPED",
                        "diagnostic_detail": "Mapped full-width header 'Amount（ksh）' -> 'amount'",
                        "auto_resolved": True,
                    }
                )

            amt_col_idx = (
                other_headers.get("amount（ksh）")
                or other_headers.get("amount(ksh)")
                or other_headers.get("amount")
                or 6
            )

            for r in range(2, ws_other.max_row + 1):
                raw_sn = ws_other.cell(r, other_headers.get("order sn") or other_headers.get("order_sn", 3)).value
                order_sn = clean_order_sn(raw_sn)
                raw_amt = ws_other.cell(r, amt_col_idx).value
                if not order_sn and (raw_amt is None or str(raw_amt).strip() == ""):
                    continue

                row_store_name = str(
                    ws_other.cell(r, other_headers.get("store name") or other_headers.get("store_name", 2)).value or ""
                ).strip()
                if row_store_name and row_store_name not in aliases_map:
                    aliases_map[row_store_name] = "Other Deductions"

                sku_str = clean_identifier(ws_other.cell(r, other_headers.get("sku", 4)).value)
                ded_type_raw = str(ws_other.cell(r, other_headers.get("type", 5)).value or "Other Deduction").strip()
                amt_dec = abs(to_decimal(raw_amt, "0.00"))
                period_str = str(ws_other.cell(r, other_headers.get("period", 7)).value or "").strip()
                note_str = str(ws_other.cell(r, other_headers.get("note", 8)).value or "").strip()

                type_low = ded_type_raw.lower()
                if "violation" in type_low:
                    deduction_code = "VIOLATION"
                    violation_deductions_sum += amt_dec
                elif "return shipping" in type_low or "after-sale" in type_low:
                    deduction_code = "AFTER_SALE_RETURN_SHIPPING"
                    return_shipping_deductions_sum += amt_dec
                else:
                    deduction_code = "OTHER_DEDUCTION"
                    other_misc_deductions_sum += amt_dec

                result.other_deductions.append(
                    {
                        "order_sn": order_sn,
                        "store_name": row_store_name or store_name,
                        "sku": sku_str,
                        "deduction_type": ded_type_raw,
                        "deduction_code": deduction_code,
                        "amount": str(amt_dec),
                        "period": period_str,
                        "note": note_str,
                    }
                )

        # Record all discovered store aliases (e.g., Kushpedia Stores4 + Harambee Ⅰ + KUSHPEDIA STORES)
        for alias_name, src_sheet in aliases_map.items():
            result.discovered_aliases.append({"alias_name": alias_name, "source_sheet": src_sheet})

        # Determine cycle start and end from timestamps
        if all_timestamps:
            sorted_ts = sorted(all_timestamps)
            result.period_start = sorted_ts[0]
            result.period_end = sorted_ts[-1]
        else:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            result.period_start = now_str
            result.period_end = now_str

        # 7. Compute Pre-Commit Cross-Week Reconciliation Impact
        positive_sales_sns = {row["order_sn"]: row for row in result.completed_sales if not row["is_return"]}
        returned_sales_sns = {row["order_sn"]: row for row in result.completed_sales if row["is_return"]}
        all_details_sns = {row["order_sn"]: row for row in result.completed_sales}
        ds_sns = {row["order_sn"]: row for row in result.ds_fees}
        prior_pending_sns = existing_pending_order_sns or set()

        same_bill_settled_sns = set(positive_sales_sns.keys()).intersection(set(ds_sns.keys()))
        prior_bill_newly_settled_sns = set(positive_sales_sns.keys()).intersection(prior_pending_sns) - same_bill_settled_sns
        prior_untracked_ds_settled_sns = set(positive_sales_sns.keys()) - same_bill_settled_sns - prior_bill_newly_settled_sns
        new_pending_settlement_sns = set(ds_sns.keys()) - set(all_details_sns.keys())

        pending_ds_fee_total = sum(
            (Decimal(ds_sns[sn]["amount"]) for sn in new_pending_settlement_sns),
            Decimal("0.00"),
        )

        package_anomalies = []
        for sale_row in result.completed_sales:
            sn = sale_row["order_sn"]
            if sale_row["is_return"]:
                sale_row["matched_ds_fee"] = ds_sns[sn]["amount"] if sn in ds_sns else None
                sale_row["matched_package_size"] = ds_sns[sn]["package_size"] if sn in ds_sns else "Return Clawback"
                sale_row["matched_shipment_no"] = ds_sns[sn]["shipment_no"] if sn in ds_sns else "Returned Item"
                sale_row["ds_match_source"] = "Customer Return (goods_num < 0)"
                continue

            if sn in ds_sns:
                ds_info = ds_sns[sn]
                sale_row["matched_ds_fee"] = ds_info["amount"]
                sale_row["matched_package_size"] = ds_info["package_size"]
                sale_row["matched_shipment_no"] = ds_info["shipment_no"]
                sale_row["ds_match_source"] = "Matched in This Bill"
                price_dec = Decimal(sale_row["complete_amount"])
                ds_dec = Decimal(ds_info["amount"])
                if price_dec > 0 and (ds_dec / price_dec) >= Decimal("0.35"):
                    ratio_pct = (ds_dec / price_dec * Decimal("100")).quantize(Decimal("0.1"))
                    sale_row["size_anomaly_flag"] = f"{ds_info['package_size']} Fee ({ds_dec} KES) is {ratio_pct}% of Price!"
                    package_anomalies.append(
                        {
                            "order_sn": sn,
                            "goods_id": sale_row["goods_id"],
                            "headline_name": sale_row["headline_name"],
                            "goods_price": sale_row["complete_amount"],
                            "package_size": ds_info["package_size"],
                            "ds_fee": ds_info["amount"],
                            "shipment_no": ds_info["shipment_no"],
                            "ratio_pct": str(ratio_pct),
                        }
                    )
            elif sn in prior_bill_newly_settled_sns:
                sale_row["matched_ds_fee"] = None
                sale_row["matched_package_size"] = "Linked from Prior Bill"
                sale_row["matched_shipment_no"] = "Prior Upload"
                sale_row["ds_match_source"] = "Auto-Settled from Pending"
            else:
                sale_row["matched_ds_fee"] = None
                sale_row["matched_package_size"] = "Billed in Prior Cycle"
                sale_row["matched_shipment_no"] = "—"
                sale_row["ds_match_source"] = "Prior Cycle DS"

        for ds_row in result.ds_fees:
            sn = ds_row["order_sn"]
            if sn in returned_sales_sns:
                ds_row["reconciliation_status"] = "RETURNED_UNSETTLED"
                ds_row["status_label"] = "Returned / Clawback"
                ds_row["matched_goods_name"] = returned_sales_sns[sn]["headline_name"]
                ds_row["matched_goods_price"] = returned_sales_sns[sn]["complete_amount"]
            elif sn in same_bill_settled_sns:
                ds_row["reconciliation_status"] = "SETTLED"
                ds_row["status_label"] = "Settled in This Bill"
                ds_row["matched_goods_name"] = positive_sales_sns[sn]["headline_name"]
                ds_row["matched_goods_price"] = positive_sales_sns[sn]["complete_amount"]
            else:
                ds_row["reconciliation_status"] = "PENDING_SETTLEMENT"
                ds_row["status_label"] = "Pending Future Settlement"
                ds_row["matched_goods_name"] = "Awaiting Delivery & Completion"
                ds_row["matched_goods_price"] = None

        new_products_count = sum(1 for p in result.discovered_products if p["is_new"])

        result.reconciliation_impact = {
            "completed_sales_count": len(positive_sales_sns),
            "returned_orders_count": len(returned_sales_sns),
            "returned_units_count": returned_units_count,
            "gross_positive_sales": str(gross_positive_sales_sum),
            "returned_sales_amount": str(returned_sales_sum),
            "returned_net_clawback": str(returned_net_clawback_sum),
            "refunded_commission": str(refunded_commission_sum),
            "ds_fees_count": len(result.ds_fees),
            "fines_count": len(result.fines),
            "other_deductions_count": len(result.other_deductions),
            "other_deductions_total": str(abs(other_deductions_total)),
            "violation_deductions_sum": str(violation_deductions_sum),
            "return_shipping_deductions_sum": str(return_shipping_deductions_sum),
            "same_bill_settled_count": len(same_bill_settled_sns),
            "prior_pending_auto_settled_count": len(prior_bill_newly_settled_sns),
            "prior_untracked_ds_settled_count": len(prior_untracked_ds_settled_sns),
            "new_pending_settlement_count": len(new_pending_settlement_sns),
            "new_pending_ds_fee_total": str(pending_ds_fee_total),
            "unique_products_count": len(result.discovered_products),
            "new_products_count": new_products_count,
            "package_anomalies_count": len(package_anomalies),
            "package_anomalies": package_anomalies,
        }

        return result
