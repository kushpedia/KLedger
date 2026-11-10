"""
Views for Executive Financial Dashboard (SCR-03), Financial Analytics & Multi-Store P&L Workspace (SCR-08),
and JSON Chart.js Data API (`apps.analytics`).
"""
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views import View

from apps.accounts.middleware import VendorRequiredMixin
from apps.accounts.models import VendorUser
from apps.products.models import Product
from apps.products.views import _enrich_products_with_order_metrics
from services.financials import compute_dashboard_and_pnl_metrics


class DashboardOverviewView(VendorRequiredMixin, View):
    """
    Implements SCR-03: Executive Financial Dashboard (Consolidated & Single-Store Views).
    Defaults to `Settlement Cash-Flow Basis` (`CASH_FLOW`) as confirmed in the architectural blueprint,
    with an instant header toggle for `Order-Matched Accrual Basis` (`ORDER_ACCRUAL`).
    """

    template_name = "analytics/dashboard.html"

    def get(self, request):
        # Operations Assistants are redirected to the Pending Shipments operational queue
        if not request.user.can_view_financials:
            return redirect("orders:pending_tracker")

        store_scope = getattr(request, "active_store_scope", "all")
        accounting_basis = getattr(request, "active_accounting_basis", "CASH_FLOW")

        metrics = compute_dashboard_and_pnl_metrics(
            tenant=request.tenant,
            store_scope=store_scope,
            accounting_basis=accounting_basis,
        )

        # Product Profitability Snapshot (Top 5 vs Margin Traps for bottom-right dashboard widget)
        prod_qs = Product.objects.filter(store__tenant=request.tenant).select_related("store")
        if store_scope and store_scope != "all":
            prod_qs = prod_qs.filter(store__kilimall_store_id=store_scope)
        enriched_skus = _enrich_products_with_order_metrics(prod_qs)
        top_skus = sorted(
            enriched_skus,
            key=lambda p: (p.total_true_profit if p.total_true_profit is not None else p.total_net_payout_after_ds),
            reverse=True,
        )[:6]

        context = {
            **metrics,
            "top_skus": top_skus,
        }
        return render(request, self.template_name, context)


class FinancialAnalyticsPnlView(VendorRequiredMixin, View):
    """
    Implements SCR-08: Financial Analytics, Dual-Basis Profit & Loss (P&L) Statement,
    Commission Tier Distribution, and Multi-Store Performance Benchmark Matrix.
    """

    template_name = "analytics/pnl_statement.html"
    allowed_roles = (VendorUser.ROLE_OWNER, VendorUser.ROLE_FINANCE)

    def get(self, request):
        store_scope = getattr(request, "active_store_scope", "all")
        accounting_basis = getattr(request, "active_accounting_basis", "CASH_FLOW")
        active_tab = request.GET.get("tab", "pnl")

        metrics = compute_dashboard_and_pnl_metrics(
            tenant=request.tenant,
            store_scope=store_scope,
            accounting_basis=accounting_basis,
        )
        metrics["active_tab"] = active_tab
        return render(request, self.template_name, metrics)


class ChartDataJsonView(VendorRequiredMixin, View):
    """Lightweight JSON endpoint returning Chart.js datasets for dynamic HTMX/AJAX chart refreshes."""

    def get(self, request):
        store_scope = getattr(request, "active_store_scope", "all")
        accounting_basis = getattr(request, "active_accounting_basis", "CASH_FLOW")
        metrics = compute_dashboard_and_pnl_metrics(
            tenant=request.tenant,
            store_scope=store_scope,
            accounting_basis=accounting_basis,
        )
        return JsonResponse({"status": "ok", "basis": accounting_basis, "charts": metrics["chart_payload_json"]})
