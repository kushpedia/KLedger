"""
Comprehensive Automated Test Suite for KiliLedger Pro (`tests/test_kililedger_suite.py`).

Tests:
1. 9-Sheet Kilimall Excel Parser (`services/parser.py`) against `bill (55).xlsx`:
   - Leading comma stripping on `order_sn` (`,610244462479107` -> `610244462479107`).
   - Exact Two-Stage Payout Equation verification (`Gross 24,260.00 -> Payable 1 20,586.78 -> Final 16,486.78`).
   - Store ID (`10344`) and secondary alias discovery (`Kushpedia Stores4` + `Harambee Ⅰ`).
   - Conversion of Excel `1900-01-13` fine durations into `13d 00h 08m`.
   - Detection of the `Super Large` (`KES 250`) package tier anomaly on Order `610272230744554`.
2. Multi-Sheet Excel (`.xlsx`) & Executive PDF (`.pdf`) Report Generators (`services/pdf_generator.py`).
"""
from decimal import Decimal
import pathlib
import unittest

from services.parser import KilimallWorkbookParser, clean_order_sn, parse_excel_spend_time
from services.pdf_generator import generate_clean_pdf_bytes, generate_multi_sheet_excel_report


SAMPLE_BILL_PATH = pathlib.Path("/working_dir/c_f45cbce6f9e21d1b/user_uploaded_files/ttl=63d/user_uploaded/bill (55).xlsx")


class KiliLedgerParserAndExportTests(unittest.TestCase):
    def test_order_sn_sanitization(self):
        self.assertEqual(clean_order_sn(",610244462479107"), "610244462479107")
        self.assertEqual(clean_order_sn('",610287144669824"'), "610287144669824")

    def test_excel_spend_time_duration_conversion(self):
        secs, badge = parse_excel_spend_time("1900-01-13 00:08:45")
        self.assertEqual(badge, "13d 00h 08m")
        self.assertEqual(secs, (13 * 86400) + (8 * 60) + 45)

    def test_full_9_sheet_parser_on_sample_bill_55(self):
        if not SAMPLE_BILL_PATH.exists():
            self.skipTest("Sample bill (55).xlsx not found in path")

        parser = KilimallWorkbookParser()
        res = parser.parse_file(str(SAMPLE_BILL_PATH), filename="bill (55).xlsx")

        self.assertTrue(res.is_valid)
        self.assertTrue(res.math_reconciled)
        self.assertEqual(res.kilimall_store_id, "10344")
        self.assertEqual(res.primary_store_name, "Kushpedia Stores4")

        alias_names = {a["alias_name"] for a in res.discovered_aliases}
        self.assertIn("Kushpedia Stores4", alias_names)
        self.assertIn("Harambee Ⅰ", alias_names)

        # Verify financial summary matches 'bill' worksheet to the cent
        self.assertEqual(Decimal(res.summary["total_volume"]), Decimal("24260.00"))
        self.assertEqual(Decimal(res.summary["total_commission"]), Decimal("-3673.22"))
        self.assertEqual(Decimal(res.summary["settlement_payable_1"]), Decimal("20586.78"))
        self.assertEqual(Decimal(res.summary["ds_processing_fee"]), Decimal("-2300.00"))
        self.assertEqual(Decimal(res.summary["fines"]), Decimal("-1800.00"))
        self.assertEqual(Decimal(res.summary["final_settlement_amount"]), Decimal("16486.78"))

        # Verify row counts & cross-sheet matching
        impact = res.reconciliation_impact
        self.assertEqual(impact["completed_sales_count"], 16)
        self.assertEqual(impact["ds_fees_count"], 22)
        self.assertEqual(impact["fines_count"], 6)
        self.assertEqual(impact["same_bill_settled_count"], 7)
        self.assertEqual(impact["new_pending_settlement_count"], 15)
        self.assertEqual(Decimal(impact["new_pending_ds_fee_total"]), Decimal("1490.00"))
        self.assertEqual(impact["unique_products_count"], 14)
        self.assertEqual(impact["package_anomalies_count"], 1)
        self.assertEqual(impact["package_anomalies"][0]["order_sn"], "610272230744554")

    def test_pdf_and_excel_report_generators(self):
        dummy_pnl = {
            "gross_sales": Decimal("24260.00"),
            "total_commission": Decimal("-3673.22"),
            "commission_pct_of_gross": Decimal("15.14"),
            "payable_1": Decimal("20586.78"),
            "payable_1_pct_of_gross": Decimal("84.86"),
            "statement_ds_fees": Decimal("-2300.00"),
            "accrual_matched_ds_fees": Decimal("-1650.00"),
            "ds_fees_pct_of_gross": Decimal("9.48"),
            "total_cogs": Decimal("9420.00"),
            "cogs_pct_of_gross": Decimal("38.83"),
            "settled_units_count": 16,
            "priced_units_count": 14,
            "order_contribution_profit": Decimal("9516.78"),
            "order_contribution_margin_pct": Decimal("39.23"),
            "statement_fines": Decimal("-1800.00"),
            "fines_pct_of_gross": Decimal("7.42"),
            "penalized_orders_count": 6,
            "net_kilimall_payout": Decimal("16486.78"),
            "payout_retention_pct": Decimal("67.96"),
            "cash_basis_profit": Decimal("7066.78"),
            "cash_basis_margin_pct": Decimal("29.13"),
            "accrual_operating_profit": Decimal("7716.78"),
            "accrual_operating_margin_pct": Decimal("31.81"),
            "displayed_margin_pct": Decimal("29.13"),
            "is_profit_incomplete": True,
            "pending_ds_fees_locked": Decimal("1490.00"),
            "pending_orders_count": 15,
            "estimated_pending_gross_value": Decimal("18400.00"),
        }

        pdf_bytes = generate_clean_pdf_bytes(
            report_title="Monthly Profit & Loss Statement",
            tenant_name="Kushpedia Enterprises",
            store_label="Kushpedia Stores4 [ID: 10344]",
            pnl_metrics=dummy_pnl,
            pending_count=15,
            fines_count=6,
        )
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4"))
        self.assertGreater(len(pdf_bytes), 800)

        xlsx_bytes = generate_multi_sheet_excel_report(
            report_code="MONTHLY_PNL",
            tenant_name="Kushpedia Enterprises",
            store_label="Kushpedia Stores4 [ID: 10344]",
            pnl_metrics=dummy_pnl,
            orders_list=[],
            pending_list=[],
            products_list=[],
            fines_list=[],
        )
        self.assertGreater(len(xlsx_bytes), 4000)


if __name__ == "__main__":
    unittest.main()
