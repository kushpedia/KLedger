"""
Views for Reports & Exports Studio (SCR-14), Live Document Preview,
and Direct PDF (.pdf) & Multi-Sheet Excel (.xlsx) Downloads (`apps.reports`).
"""
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from apps.accounts.middleware import VendorRequiredMixin
from apps.orders.models import Order, PenaltyFine
from apps.products.models import Product
from apps.products.views import _enrich_products_with_order_metrics
from apps.reports.models import GeneratedReportArchive
from services.financials import compute_dashboard_and_pnl_metrics
from services.pdf_generator import (
    REPORT_TEMPLATES_CATALOG,
    generate_clean_pdf_bytes,
    generate_multi_sheet_excel_report,
)


class ReportStudioCenterView(VendorRequiredMixin, View):
    """
    Implements SCR-14: Reports & Exports Center (PDF & Excel Builder with Live A4 Preview).
    Supports selecting any of the 9 commercial report templates, store scope, and accounting basis,
    previewing the live report on screen, and downloading `.pdf` or `.xlsx`.
    """

    template_name = "reports/report_center.html"

    def _gather_report_payload(self, request):
        store_scope = request.GET.get("store") or getattr(request, "active_store_scope", "all")
        accounting_basis = request.GET.get("basis") or getattr(request, "active_accounting_basis", "CASH_FLOW")
        selected_code = request.GET.get("report", "MONTHLY_PNL")

        selected_template = next(
            (t for t in REPORT_TEMPLATES_CATALOG if t["code"] == selected_code),
            REPORT_TEMPLATES_CATALOG[0],
        )

        pnl_metrics = compute_dashboard_and_pnl_metrics(
            tenant=request.tenant,
            store_scope=store_scope,
            accounting_basis=accounting_basis,
        )

        orders_qs = Order.objects.filter(store__tenant=request.tenant).select_related("store")
        fines_qs = PenaltyFine.objects.filter(order__store__tenant=request.tenant).select_related("order", "order__store")
        products_qs = Product.objects.filter(store__tenant=request.tenant).select_related("store")

        if store_scope and store_scope != "all":
            orders_qs = orders_qs.filter(store__kilimall_store_id=store_scope)
            fines_qs = fines_qs.filter(order__store__kilimall_store_id=store_scope)
            products_qs = products_qs.filter(store__kilimall_store_id=store_scope)
            store_obj = request.tenant.stores.filter(kilimall_store_id=store_scope).first()
            store_label = f"{store_obj.store_name} [ID: {store_obj.kilimall_store_id}]" if store_obj else f"Store {store_scope}"
        else:
            store_label = f"All My Stores ({request.tenant.stores.count()} Active — Consolidated)"

        pending_list = list(
            orders_qs.filter(settlement_status__in=[Order.STATUS_PENDING, Order.STATUS_OVERDUE]).order_by("ds_fee_scan_date")
        )
        enriched_products = _enrich_products_with_order_metrics(products_qs)

        return {
            "selected_template": selected_template,
            "selected_code": selected_template["code"],
            "store_scope": store_scope,
            "store_label": store_label,
            "accounting_basis": accounting_basis,
            "pnl": pnl_metrics,
            "orders_list": list(orders_qs[:200]),
            "pending_list": pending_list,
            "products_list": enriched_products,
            "fines_list": list(fines_qs),
        }

    def get(self, request):
        export_format = request.GET.get("download")
        payload = self._gather_report_payload(request)

        # Direct Download Handler: ?download=xlsx or ?download=pdf
        if export_format in ("xlsx", "pdf"):
            fmt_upper = "XLSX" if export_format == "xlsx" else "PDF"
            GeneratedReportArchive.objects.create(
                tenant=request.tenant,
                report_code=payload["selected_code"],
                report_title=payload["selected_template"]["title"],
                store_scope_label=payload["store_label"],
                accounting_basis=payload["accounting_basis"],
                export_format=fmt_upper,
                generated_by=request.user,
            )

            slug = payload["selected_code"].lower()
            if export_format == "xlsx":
                xlsx_bytes = generate_multi_sheet_excel_report(
                    report_code=payload["selected_code"],
                    tenant_name=request.tenant.business_name,
                    store_label=payload["store_label"],
                    pnl_metrics=payload["pnl"],
                    orders_list=payload["orders_list"],
                    pending_list=payload["pending_list"],
                    products_list=payload["products_list"],
                    fines_list=payload["fines_list"],
                )
                response = HttpResponse(
                    xlsx_bytes,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                response["Content-Disposition"] = f'attachment; filename="kililedger_{slug}_report.xlsx"'
                return response

            pdf_bytes = generate_clean_pdf_bytes(
                report_title=payload["selected_template"]["title"],
                tenant_name=request.tenant.business_name,
                store_label=payload["store_label"],
                pnl_metrics=payload["pnl"],
                pending_count=len(payload["pending_list"]),
                fines_count=len(payload["fines_list"]),
            )
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="kililedger_{slug}_report.pdf"'
            return response

        recent_exports = GeneratedReportArchive.objects.filter(tenant=request.tenant)[:12]
        return render(
            request,
            self.template_name,
            {
                **payload,
                "report_templates": REPORT_TEMPLATES_CATALOG,
                "recent_exports": recent_exports,
            },
        )
