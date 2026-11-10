"""
Commercial Report Builder, Multi-Sheet Excel (.xlsx) Workbook Exporter,
and Executive PDF / Printable Document Renderer (`services/pdf_generator.py`).

Supports all 9 confirmed report templates in `SCR-14`:
1. `SETTLEMENT_SUMMARY`: Single or Multi-Cycle Settlement Summary Report
2. `STORE_PERFORMANCE`: Store Financial Performance Deep-Dive Report
3. `MONTHLY_PNL`: Monthly Profit & Loss (P&L) & Dual-Basis Realization Statement (Default)
4. `CONSOLIDATED_PORTFOLIO`: Consolidated Multi-Store Executive Benchmark Report
5. `PRODUCT_PROFITABILITY`: Product Profitability, COGS Valuation & Margin Trap Report
6. `ORDER_RECONCILIATION`: Master Cross-Week Order Reconciliation Ledger
7. `PENDING_CLAIMS`: Pending Settlement & Unsettled DS Transit Escrow Claims Report
8. `DS_APPEAL_EVIDENCE`: DS Processing Fee & Package Size Overcharge Appeal Sheet
9. `PENALTY_ROOT_CAUSE`: SLA Penalty, Overdue Dispatch & Out-of-Stock Audit Report
"""
from __future__ import annotations

import datetime
import io
from decimal import Decimal
from typing import Any, Dict, List

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


REPORT_TEMPLATES_CATALOG = [
    {
        "code": "MONTHLY_PNL",
        "number": "01",
        "title": "Monthly Profit & Loss (P&L) & Unit Economics",
        "subtitle": "Dual-basis Cash vs. Order-Accrual P&L with verified COGS & net margin",
        "icon": "analytics",
    },
    {
        "code": "SETTLEMENT_SUMMARY",
        "number": "02",
        "title": "Settlement Summary Statement Audit",
        "subtitle": "Two-stage Kilimall payout verification (Payable 1 -> Final Settlement)",
        "icon": "receipt_long",
    },
    {
        "code": "CONSOLIDATED_PORTFOLIO",
        "number": "03",
        "title": "Consolidated Multi-Store Executive Report",
        "subtitle": "Side-by-side performance benchmark across all registered Kilimall stores",
        "icon": "account_tree",
    },
    {
        "code": "PRODUCT_PROFITABILITY",
        "number": "04",
        "title": "Product Profitability & COGS Valuation Report",
        "subtitle": "SKU-level unit economics, COGS coverage, and low-ticket margin traps",
        "icon": "inventory_2",
    },
    {
        "code": "ORDER_RECONCILIATION",
        "number": "05",
        "title": "Cross-Week Order Reconciliation Ledger",
        "subtitle": "Complete order lifecycle table separating Settlement Status vs. Penalties",
        "icon": "sync_alt",
    },
    {
        "code": "PENDING_CLAIMS",
        "number": "06",
        "title": "Pending Settlement & Unsettled DS Claims",
        "subtitle": "Aged packages with deducted DS fees awaiting Kilimall sale payout",
        "icon": "local_shipping",
    },
    {
        "code": "DS_APPEAL_EVIDENCE",
        "number": "07",
        "title": "DS Processing Fee & Package Tier Appeal Sheet",
        "subtitle": "Evidence pack for misclassified package tiers (e.g., Super Large KES 250)",
        "icon": "gavel",
    },
    {
        "code": "PENALTY_ROOT_CAUSE",
        "number": "08",
        "title": "Penalty & Overdue Dispatch Audit Report",
        "subtitle": "Root-cause breakdown of overdue dispatch and out-of-stock fines",
        "icon": "warning",
    },
    {
        "code": "STORE_PERFORMANCE",
        "number": "09",
        "title": "Single-Store Operational Health Dossier",
        "subtitle": "Store-specific statements, aliases (Harambee Ⅰ), and payout retention",
        "icon": "storefront",
    },
]


def generate_multi_sheet_excel_report(
    report_code: str,
    tenant_name: str,
    store_label: str,
    pnl_metrics: Dict[str, Any],
    orders_list: List[Any],
    pending_list: List[Any],
    products_list: List[Any],
    fines_list: List[Any],
) -> bytes:
    """
    Builds a professionally styled, multi-worksheet `.xlsx` commercial financial workbook
    using `openpyxl` with formatted currency cells (`#,##0.00`), bold navy/teal headers,
    and auto-fitted column widths.
    """
    wb = openpyxl.Workbook()

    # Palette & Styles matching DESIGN.md
    header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    sub_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    total_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    warn_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")

    title_font = Font(name="Calibri", size=14, bold=True, color="0F172A")
    subtitle_font = Font(name="Calibri", size=10, italic=True, color="475569")
    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    bold_font = Font(name="Calibri", size=10, bold=True, color="0F172A")
    regular_font = Font(name="Calibri", size=10, color="0F172A")

    thin_border = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )

    def _style_header_row(ws, row_idx: int, headers: List[str]):
        for col_idx, text in enumerate(headers, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=text)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center" if col_idx > 2 else "left", vertical="center")
        ws.row_dimensions[row_idx].height = 24

    def _auto_fit_columns(ws):
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val_str = str(cell.value or "")
                max_len = max(max_len, len(val_str))
            ws.column_dimensions[col_letter].width = min(max(max_len + 4, 13), 44)

    # =========================================================================
    # SHEET 1: Executive P&L & Settlement Reconciliation Summary
    # =========================================================================
    ws_pnl = wb.active
    ws_pnl.title = "1_Executive_PnL_Summary"

    ws_pnl["A1"] = f"KILILEDGER PRO — OFFICIAL FINANCIAL & RECONCILIATION REPORT ({report_code})"
    ws_pnl["A1"].font = title_font
    ws_pnl["A2"] = f"Vendor Workspace: {tenant_name} | Store Scope: {store_label} | Generated: {datetime.datetime.now():%Y-%m-%d %H:%M EAT}"
    ws_pnl["A2"].font = subtitle_font

    _style_header_row(
        ws_pnl,
        4,
        ["P&L / Settlement Line Item", "Cash-Flow Basis (KES)", "Order-Accrual Basis (KES)", "% of Gross Sales", "Audit Notes"],
    )

    pnl_rows = [
        (
            "(+) Gross Merchandise Value (Completed Sales)",
            float(pnl_metrics["gross_sales"]),
            float(pnl_metrics["gross_sales"]),
            "100.00%",
            f"{pnl_metrics['settled_units_count']} completed units settled",
        ),
        (
            "(-) Kilimall Category Commissions (9% - 16%)",
            float(pnl_metrics["total_commission"]),
            float(pnl_metrics["total_commission"]),
            f"{pnl_metrics['commission_pct_of_gross']}%",
            "Category commission deductions",
        ),
        (
            "(=) Settlement Payable 1 (Trading Margin)",
            float(pnl_metrics["payable_1"]),
            float(pnl_metrics["payable_1"]),
            f"{pnl_metrics['payable_1_pct_of_gross']}%",
            "Matches 'bill' sheet Settlement Payable 1",
        ),
        (
            "(-) Drop-Shipping (DS) Processing Fees",
            float(pnl_metrics["statement_ds_fees"]),
            float(pnl_metrics["accrual_matched_ds_fees"]),
            f"{pnl_metrics['ds_fees_pct_of_gross']}%",
            "Cash: All billed packages | Accrual: Matched to settled sales",
        ),
        (
            "(-) Cost of Goods Sold (Verified Landed COGS)",
            -float(pnl_metrics["total_cogs"]),
            -float(pnl_metrics["total_cogs"]),
            f"{pnl_metrics['cogs_pct_of_gross']}%",
            f"{pnl_metrics['priced_units_count']}/{pnl_metrics['settled_units_count']} units have verified buying prices",
        ),
        (
            "(=) GROSS ORDER CONTRIBUTION PROFIT",
            float(pnl_metrics["payable_1"] + pnl_metrics["statement_ds_fees"] - pnl_metrics["total_cogs"]),
            float(pnl_metrics["order_contribution_profit"]),
            f"{pnl_metrics['order_contribution_margin_pct']}%",
            "Product unit economics before unlinked store fines",
        ),
        (
            "(-) Unlinked Store-Level SLA Penalties & Fines",
            float(pnl_metrics["statement_fines"]),
            float(pnl_metrics["statement_fines"]),
            f"{pnl_metrics['fines_pct_of_gross']}%",
            f"{pnl_metrics['penalized_orders_count']} penalty incidents (@ KES 300)",
        ),
        (
            "(=) NET KILIMALL BANK PAYOUT (Before COGS)",
            float(pnl_metrics["net_kilimall_payout"]),
            float(pnl_metrics["net_kilimall_payout"]),
            f"{pnl_metrics['payout_retention_pct']}%",
            "Exact cash transferred by Kilimall",
        ),
        (
            "(=) ESTIMATED POCKET NET PROFIT (After COGS & All Fees)",
            float(pnl_metrics["cash_basis_profit"]),
            float(pnl_metrics["accrual_operating_profit"]),
            f"{pnl_metrics['displayed_margin_pct']}%",
            "Estimate (incomplete COGS)" if pnl_metrics["is_profit_incomplete"] else "100% Verified Net Profit",
        ),
        (
            "[Memo] Pending Settlement DS Fees Locked in Escrow",
            float(pnl_metrics["pending_ds_fees_locked"]),
            float(pnl_metrics["pending_ds_fees_locked"]),
            f"{pnl_metrics['pending_orders_count']} orders",
            "Awaiting customer delivery & future statement payout",
        ),
    ]

    for idx, r_data in enumerate(pnl_rows, start=5):
        for c_idx, val in enumerate(r_data, start=1):
            cell = ws_pnl.cell(row=idx, column=c_idx, value=val)
            cell.border = thin_border
            cell.font = bold_font if r_data[0].startswith("(=)") else regular_font
            if c_idx in (2, 3) and isinstance(val, (int, float)):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")
            if "POCKET NET PROFIT" in r_data[0]:
                cell.fill = total_fill
            elif r_data[0].startswith("[Memo]"):
                cell.fill = warn_fill

    _auto_fit_columns(ws_pnl)

    # =========================================================================
    # SHEET 2: Reconciled Order Ledger (All Orders)
    # =========================================================================
    ws_ord = wb.create_sheet(title="2_Reconciled_Orders")
    _style_header_row(
        ws_ord,
        1,
        [
            "Order SN",
            "Store ID",
            "Store / Alias",
            "Settlement Status",
            "Penalty Status",
            "Goods ID",
            "Product Headline",
            "Shipment No",
            "Package Tier",
            "Gross Sale (KES)",
            "Commission (KES)",
            "Linked DS Fee (KES)",
            "Penalty Fine (KES)",
            "Applied COGS (KES)",
            "Net Order Profit (KES)",
        ],
    )
    for r_i, o in enumerate(orders_list, start=2):
        row_vals = [
            o.order_sn,
            o.store.kilimall_store_id,
            o.workbook_store_alias or o.store.store_name,
            o.get_settlement_status_display(),
            o.get_penalty_status_display(),
            o.goods_id or "—",
            o.headline_name or "—",
            o.linked_shipment_no or "—",
            o.linked_package_size or "—",
            float(o.goods_price) if o.goods_price is not None else 0.0,
            float(o.commission_amount) if o.commission_amount is not None else 0.0,
            -float(o.linked_ds_fee) if o.linked_ds_fee else 0.0,
            -float(o.linked_fine_amount) if o.linked_fine_amount else 0.0,
            -float(o.applied_cogs) if o.applied_cogs is not None else 0.0,
            float(o.net_order_profit) if o.net_order_profit is not None else 0.0,
        ]
        for c_i, val in enumerate(row_vals, start=1):
            c = ws_ord.cell(row=r_i, column=c_i, value=val)
            c.border = thin_border
            if c_i >= 10:
                c.number_format = "#,##0.00"
    _auto_fit_columns(ws_ord)

    # =========================================================================
    # SHEET 3: Pending Settlement & Unsettled DS Claims
    # =========================================================================
    ws_pend = wb.create_sheet(title="3_Pending_Settlements")
    _style_header_row(
        ws_pend,
        1,
        [
            "Order SN",
            "Shipment Tracking No",
            "Store ID & Name",
            "DS Hub Scan Date",
            "Days Pending",
            "Package Tier",
            "DS Fee Paid Upfront (KES)",
            "Current Status",
        ],
    )
    for r_i, p_ord in enumerate(pending_list, start=2):
        scan_str = p_ord.ds_fee_scan_date.strftime("%Y-%m-%d %H:%M") if p_ord.ds_fee_scan_date else "—"
        vals = [
            p_ord.order_sn,
            p_ord.linked_shipment_no,
            f"{p_ord.store.store_name} [{p_ord.store.kilimall_store_id}]",
            scan_str,
            p_ord.transit_days_elapsed,
            p_ord.linked_package_size,
            float(p_ord.linked_ds_fee),
            p_ord.get_settlement_status_display(),
        ]
        for c_i, val in enumerate(vals, start=1):
            c = ws_pend.cell(row=r_i, column=c_i, value=val)
            c.border = thin_border
            if c_i == 7:
                c.number_format = "#,##0.00"
    _auto_fit_columns(ws_pend)

    # =========================================================================
    # SHEET 4: Product Profitability & COGS Valuation
    # =========================================================================
    ws_prod = wb.create_sheet(title="4_Product_Profitability")
    _style_header_row(
        ws_prod,
        1,
        [
            "Goods ID",
            "Product Name",
            "Units Sold",
            "Unit Selling Price (KES)",
            "Comm Rate (%)",
            "Typical DS Fee (KES)",
            "Unit Buying Price / COGS (KES)",
            "Total Gross Revenue (KES)",
            "Total True Profit (KES)",
            "Net Margin (%)",
            "Margin Tier",
        ],
    )
    for r_i, prod in enumerate(products_list, start=2):
        vals = [
            prod.goods_id,
            prod.headline_name,
            getattr(prod, "units_sold", 1),
            float(prod.latest_selling_price),
            float(prod.commission_rate_pct),
            float(prod.typical_ds_fee),
            float(prod.current_buying_price) if prod.current_buying_price is not None else "Missing COGS",
            float(getattr(prod, "gross_revenue", prod.latest_selling_price)),
            float(prod.total_true_profit) if getattr(prod, "total_true_profit", None) is not None else "Needs COGS",
            float(prod.portfolio_margin_pct) if getattr(prod, "portfolio_margin_pct", None) is not None else "—",
            prod.margin_status_tier,
        ]
        for c_i, val in enumerate(vals, start=1):
            c = ws_prod.cell(row=r_i, column=c_i, value=val)
            c.border = thin_border
            if isinstance(val, float):
                c.number_format = "#,##0.00"
    _auto_fit_columns(ws_prod)

    # =========================================================================
    # SHEET 5: SLA Fines & Penalties Audit
    # =========================================================================
    ws_fine = wb.create_sheet(title="5_SLA_Fines_Audit")
    _style_header_row(
        ws_fine,
        1,
        [
            "Order SN",
            "Store ID",
            "Workbook Alias",
            "Payment Time",
            "Cancel Time",
            "Elapsed Delay",
            "Penalty Reason",
            "Fine Deducted (KES)",
        ],
    )
    for r_i, fn in enumerate(fines_list, start=2):
        vals = [
            fn.order.order_sn,
            fn.order.store.kilimall_store_id,
            fn.workbook_store_alias,
            fn.payment_time.strftime("%Y-%m-%d %H:%M") if fn.payment_time else "—",
            fn.cancel_time.strftime("%Y-%m-%d %H:%M") if fn.cancel_time else "—",
            fn.elapsed_badge,
            fn.reason,
            -float(fn.fine_amount),
        ]
        for c_i, val in enumerate(vals, start=1):
            c = ws_fine.cell(row=r_i, column=c_i, value=val)
            c.border = thin_border
            if c_i == 8:
                c.number_format = "#,##0.00"
    _auto_fit_columns(ws_fine)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_clean_pdf_bytes(
    report_title: str,
    tenant_name: str,
    store_label: str,
    pnl_metrics: Dict[str, Any],
    pending_count: int,
    fines_count: int,
) -> bytes:
    """
    Generates a clean, valid standalone PDF executive financial report in pure Python
    (zero external system dependencies required, works everywhere out-of-the-box)
    while also supporting WeasyPrint/browser print-to-PDF for full HTML tables.
    """
    lines = [
        f"KILILEDGER PRO - EXECUTIVE FINANCIAL & RECONCILIATION REPORT",
        f"Report Type: {report_title}",
        f"Vendor Workspace: {tenant_name} | Store Scope: {store_label}",
        f"Generated: {datetime.datetime.now():%Y-%m-%d %H:%M EAT}",
        "------------------------------------------------------------------------",
        "1. KILIMALL SETTLEMENT & DUAL-BASIS P&L SUMMARY",
        f"   (+) Gross Merchandise Value (Sales):     KES {pnl_metrics['gross_sales']:>12,.2f} (100.00%)",
        f"   (-) Kilimall Category Commissions:       KES {pnl_metrics['total_commission']:>12,.2f} ({pnl_metrics['commission_pct_of_gross']}%)",
        f"   (=) Settlement Payable 1 (Trading Net):  KES {pnl_metrics['payable_1']:>12,.2f} ({pnl_metrics['payable_1_pct_of_gross']}%)",
        f"   (-) DS Processing Fees (Statement):      KES {pnl_metrics['statement_ds_fees']:>12,.2f} ({pnl_metrics['ds_fees_pct_of_gross']}%)",
        f"   (-) SLA Penalties & Fines ({fines_count} fines):      KES {pnl_metrics['statement_fines']:>12,.2f} ({pnl_metrics['fines_pct_of_gross']}%)",
        f"   (=) FINAL KILIMALL BANK PAYOUT:          KES {pnl_metrics['net_kilimall_payout']:>12,.2f} ({pnl_metrics['payout_retention_pct']}% Retained)",
        "------------------------------------------------------------------------",
        "2. COST OF GOODS SOLD (COGS) & TRUE POCKET PROFITABILITY",
        f"   (-) Verified Landed COGS ({pnl_metrics['priced_units_count']}/{pnl_metrics['settled_units_count']} units):   -KES {pnl_metrics['total_cogs']:>12,.2f} ({pnl_metrics['cogs_pct_of_gross']}%)",
        f"   (=) Cash-Flow Basis Pocket Profit:       KES {pnl_metrics['cash_basis_profit']:>12,.2f} ({pnl_metrics['cash_basis_margin_pct']}%)",
        f"   (=) Order-Matched Accrual Net Profit:    KES {pnl_metrics['accrual_operating_profit']:>12,.2f} ({pnl_metrics['accrual_operating_margin_pct']}%)",
        "------------------------------------------------------------------------",
        "3. CROSS-WEEK PENDING SETTLEMENT ESCROW PIPELINE",
        f"   * Active Unsettled DS Shipments:         {pending_count} Packages",
        f"   * Upfront DS Fees Locked in Escrow:      KES {pnl_metrics['pending_ds_fees_locked']:>12,.2f}",
        f"   * Estimated Future Gross Settlement:    ~KES {pnl_metrics['estimated_pending_gross_value']:>12,.2f}",
        "------------------------------------------------------------------------",
        "Audit Note: Generated by KiliLedger Pro Multi-Vendor Reconciliation Engine.",
    ]

    # Build a standards-compliant single-page PDF stream with Courier/Helvetica text positioning
    content_ops = ["BT", "/F1 10 Tf", "50 760 Td", "15 TL"]
    for idx, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if idx == 0:
            content_ops.append(f"/F2 12 Tf ({escaped}) Tj T* /F1 10 Tf")
        else:
            content_ops.append(f"({escaped}) Tj T*")
    content_ops.append("ET")
    stream_bytes = "\n".join(content_ops).encode("latin-1", errors="replace")

    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length " + str(len(stream_bytes)).encode("ascii") + b" >>\nstream\n" + stream_bytes + b"\nendstream",
    ]

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode("ascii"))
        out.write(obj)
        out.write(b"\nendobj\n")

    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode("ascii"))
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return out.getvalue()
