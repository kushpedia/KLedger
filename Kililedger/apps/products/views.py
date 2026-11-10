"""
Views for Product Catalogue & Inline HTMX Buying Price (COGS) Manager (SCR-11),
Bulk CSV COGS Import/Template Export, Cost History Slide-Over Drawer,
and Product Profitability Leaderboard & Margin Trap Analyzer (SCR-12).
"""
import csv
import io
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.accounts.middleware import VendorRequiredMixin
from apps.accounts.models import VendorUser
from apps.orders.models import Order
from apps.products.forms import BulkCogsCsvUploadForm, InlineBuyingPriceForm
from apps.products.models import Product


def _enrich_products_with_order_metrics(products_qs):
    """
    Attaches aggregated settled sales metrics (`units_sold`, `gross_revenue`, `total_commission`,
    `total_linked_ds_fees`, `total_net_settlement`, `total_cogs`, `total_true_profit`)
    to each `Product` instance from matched `Order` rows.
    """
    products = list(products_qs)
    if not products:
        return []

    store_ids = {p.store_id for p in products}
    goods_ids = {p.goods_id for p in products}

    order_aggs = (
        Order.objects.filter(
            store_id__in=store_ids,
            goods_id__in=goods_ids,
            settlement_status=Order.STATUS_SETTLED,
        )
        .values("store_id", "goods_id")
        .annotate(
            orders_count=Count("id"),
            units_sold=Sum("goods_num"),
            gross_revenue=Sum("goods_price"),
            total_commission=Sum("commission_amount"),
            total_ds_fees=Sum("linked_ds_fee"),
            total_net_settlement=Sum("net_settlement"),
            total_applied_cogs=Sum("applied_cogs"),
        )
    )
    agg_map = {(r["store_id"], r["goods_id"]): r for r in order_aggs}

    # Also inspect any typical DS fee from orders where linked_ds_fee > 0
    ds_orders = Order.objects.filter(
        store_id__in=store_ids,
        goods_id__in=goods_ids,
        linked_ds_fee__gt=0,
    ).values("store_id", "goods_id", "linked_ds_fee", "linked_package_size")
    ds_map = {(r["store_id"], r["goods_id"]): r for r in ds_orders}

    for p in products:
        key = (p.store_id, p.goods_id)
        ds_info = ds_map.get(key)
        if ds_info:
            p.typical_ds_fee = ds_info["linked_ds_fee"]
            p.typical_package_size = ds_info["linked_package_size"] or "Medium"

        agg = agg_map.get(key, {})
        p.units_sold = agg.get("units_sold") or 1
        p.gross_revenue = agg.get("gross_revenue") or p.latest_selling_price
        p.total_commission = agg.get("total_commission") or -p.unit_commission_amount
        # Use actual linked DS fee if recorded on settled orders, or estimate from typical_ds_fee * units_sold
        recorded_ds = agg.get("total_ds_fees") or Decimal("0.00")
        p.total_ds_fees = recorded_ds if recorded_ds > 0 else (p.typical_ds_fee * p.units_sold)
        p.total_net_payout_after_ds = (p.gross_revenue - abs(p.total_commission) - p.total_ds_fees).quantize(Decimal("0.01"))

        if p.current_buying_price is not None:
            p.total_cogs = (p.current_buying_price * p.units_sold).quantize(Decimal("0.01"))
            p.total_true_profit = (p.total_net_payout_after_ds - p.total_cogs).quantize(Decimal("0.01"))
            p.portfolio_margin_pct = (
                (p.total_true_profit / p.gross_revenue * Decimal("100")).quantize(Decimal("0.1"))
                if p.gross_revenue > 0
                else Decimal("0.0")
            )
        else:
            p.total_cogs = None
            p.total_true_profit = None
            p.portfolio_margin_pct = None

    return products


class ProductCatalogueView(VendorRequiredMixin, View):
    """
    Implements SCR-11: Product Catalogue & HTMX Inline Buying Price (COGS) Management.
    """

    template_name = "products/product_catalogue.html"

    def get(self, request):
        base_qs = Product.objects.filter(store__tenant=request.tenant).select_related("store").prefetch_related("cost_history")
        if request.active_store_scope and request.active_store_scope != "all":
            base_qs = base_qs.filter(store__kilimall_store_id=request.active_store_scope)

        total_skus = base_qs.count()
        priced_skus = base_qs.filter(current_buying_price__isnull=False).count()
        missing_skus = total_skus - priced_skus
        coverage_pct = int(round((priced_skus / total_skus) * 100)) if total_skus > 0 else 100

        cogs_filter = request.GET.get("filter", "all")
        rate_filter = request.GET.get("rate", "all")
        search_q = request.GET.get("q", "").strip()

        filtered_qs = base_qs
        if cogs_filter == "missing_cogs":
            filtered_qs = filtered_qs.filter(current_buying_price__isnull=True)
        elif cogs_filter == "priced":
            filtered_qs = filtered_qs.filter(current_buying_price__isnull=False)

        if rate_filter != "all":
            try:
                rate_dec = Decimal(rate_filter)
                filtered_qs = filtered_qs.filter(category_commission_rate=rate_dec)
            except InvalidOperation:
                pass

        if search_q:
            filtered_qs = filtered_qs.filter(
                Q(goods_id__icontains=search_q) | Q(goods_name__icontains=search_q) | Q(headline_name__icontains=search_q)
            )

        enriched_products = _enrich_products_with_order_metrics(filtered_qs)

        return render(
            request,
            self.template_name,
            {
                "products": enriched_products,
                "total_skus": total_skus,
                "priced_skus": priced_skus,
                "missing_skus": missing_skus,
                "coverage_pct": coverage_pct,
                "cogs_filter": cogs_filter,
                "rate_filter": rate_filter,
                "search_q": search_q,
                "bulk_form": BulkCogsCsvUploadForm(),
            },
        )


class InlineCogsUpdatePartialView(VendorRequiredMixin, View):
    """
    HTMX endpoint (`POST /products/<uuid:pk>/update-cogs/`) that saves a product's wholesale
    Buying Price (`current_buying_price`), appends a `ProductCostHistory` version, updates
    linked `Order` profits, and returns the updated `<tr>` row partial with a green `✓ Saved` badge.
    """

    template_name = "components/_inline_cogs_cell.html"
    allowed_roles = (VendorUser.ROLE_OWNER, VendorUser.ROLE_FINANCE)

    def post(self, request, pk):
        product = get_object_or_404(Product.objects.select_related("store"), pk=pk, store__tenant=request.tenant)
        form = InlineBuyingPriceForm(request.POST)

        saved_just_now = False
        error_msg = None

        if form.is_valid():
            new_price = form.cleaned_data["buying_price"]
            note = form.cleaned_data.get("supplier_note") or "Inline HTMX COGS update"
            product.set_buying_price(
                new_price=new_price,
                user=request.user,
                supplier_note=note,
                backfill_unpriced_orders=True,
            )
            saved_just_now = True
        else:
            error_msg = next(iter(form.errors.values()))[0] if form.errors else "Invalid buying price"

        enriched = _enrich_products_with_order_metrics([product])[0]

        if request.headers.get("HX-Request"):
            return render(
                request,
                self.template_name,
                {
                    "product": enriched,
                    "saved_just_now": saved_just_now,
                    "error_msg": error_msg,
                },
            )

        if saved_just_now:
            messages.success(
                request,
                f"Updated buying price for '{product.headline_name[:40]}' (SKU {product.goods_id}) to KES {product.current_buying_price:,.2f}.",
            )
        else:
            messages.error(request, f"Could not update buying price: {error_msg}")
        return redirect("products:product_catalogue")


class ProductCostHistoryDrawerView(VendorRequiredMixin, View):
    """
    HTMX Slide-Over Drawer (`GET /products/<uuid:pk>/history-drawer/`) showing historical buying price
    versions (`ProductCostHistory`) and unit sales history for a specific SKU (`SCR-11`).
    """

    template_name = "products/_cost_history_drawer.html"

    def get(self, request, pk):
        product = get_object_or_404(
            Product.objects.select_related("store").prefetch_related("cost_history__updated_by"),
            pk=pk,
            store__tenant=request.tenant,
        )
        enriched = _enrich_products_with_order_metrics([product])[0]
        sku_orders = Order.objects.filter(store=product.store, goods_id=product.goods_id).order_by("-payment_time")[:20]

        return render(
            request,
            self.template_name,
            {
                "product": enriched,
                "cost_history": product.cost_history.all(),
                "sku_orders": sku_orders,
            },
        )


class BulkCogsCsvImportExportView(VendorRequiredMixin, View):
    """
    Supports downloading a pre-filled CSV template of all discovered `goods_id` SKUs (`GET`)
    and bulk-importing wholesale buying prices via CSV (`POST`).
    """

    allowed_roles = (VendorUser.ROLE_OWNER, VendorUser.ROLE_FINANCE)

    def get(self, request):
        qs = Product.objects.filter(store__tenant=request.tenant).select_related("store")
        if request.active_store_scope and request.active_store_scope != "all":
            qs = qs.filter(store__kilimall_store_id=request.active_store_scope)

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="kililedger_product_cogs_template.csv"'

        writer = csv.writer(response)
        writer.writerow(["goods_id", "store_id", "product_name", "selling_price_kes", "buying_price", "supplier_note"])
        for p in qs:
            writer.writerow(
                [
                    p.goods_id,
                    p.store.kilimall_store_id,
                    p.headline_name,
                    str(p.latest_selling_price),
                    str(p.current_buying_price or ""),
                    "Wholesale landed cost",
                ]
            )
        return response

    def post(self, request):
        form = BulkCogsCsvUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Please upload a valid .csv file.")
            return redirect("products:product_catalogue")

        csv_file = form.cleaned_data["csv_file"]
        decoded = csv_file.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(decoded))

        updated_count = 0
        for row in reader:
            gid = str(row.get("goods_id") or "").strip()
            raw_price = str(row.get("buying_price") or "").strip()
            note = str(row.get("supplier_note") or "Bulk CSV import").strip()
            if not gid or not raw_price:
                continue
            try:
                price_dec = Decimal(raw_price)
                if price_dec <= 0:
                    continue
            except InvalidOperation:
                continue

            matching_products = Product.objects.filter(store__tenant=request.tenant, goods_id=gid)
            for prod in matching_products:
                prod.set_buying_price(new_price=price_dec, user=request.user, supplier_note=note)
                updated_count += 1

        messages.success(request, f"Bulk COGS import complete: Updated buying prices for {updated_count} SKU(s).")
        return redirect("products:product_catalogue")


class ProductProfitabilityLeaderboardView(VendorRequiredMixin, View):
    """
    Implements SCR-12: Product Profitability Leaderboard, Unit Economics Decomposition,
    Low-Ticket DS Fee Margin Trap Diagnostics, and Interactive Price Simulator.
    """

    template_name = "products/profitability_rank.html"
    allowed_roles = (VendorUser.ROLE_OWNER, VendorUser.ROLE_FINANCE)

    def get(self, request):
        base_qs = Product.objects.filter(store__tenant=request.tenant).select_related("store")
        if request.active_store_scope and request.active_store_scope != "all":
            base_qs = base_qs.filter(store__kilimall_store_id=request.active_store_scope)

        all_enriched = _enrich_products_with_order_metrics(base_qs)

        # Sort priced products by total_true_profit descending, placing unpriced SKUs at the bottom
        priced_list = sorted(
            [p for p in all_enriched if p.total_true_profit is not None],
            key=lambda x: x.total_true_profit,
            reverse=True,
        )
        unpriced_list = [p for p in all_enriched if p.total_true_profit is None]

        # Identify Margin Traps & Low-Ticket DS Fee Anomalies (e.g. Melamine Mug 10006935691 & Washing Machine Pads 10006158974)
        margin_traps = [
            p for p in all_enriched
            if p.ds_fee_to_price_ratio_pct >= Decimal("30.0") or p.margin_status_tier in ("MARGIN_TRAP", "LOSS_MAKING")
        ]

        tier_filter = request.GET.get("tier", "all")
        if tier_filter == "high_margin":
            display_products = [p for p in priced_list if p.margin_status_tier == "HIGH_MARGIN"]
        elif tier_filter == "margin_trap":
            display_products = margin_traps
        elif tier_filter == "loss_making":
            display_products = [p for p in priced_list if p.margin_status_tier == "LOSS_MAKING"]
        elif tier_filter == "missing_cogs":
            display_products = unpriced_list
        else:
            display_products = priced_list + unpriced_list

        top_sku = priced_list[0] if priced_list else (all_enriched[0] if all_enriched else None)
        total_priced_revenue = sum((p.gross_revenue for p in priced_list), Decimal("0.00"))
        total_priced_profit = sum((p.total_true_profit for p in priced_list), Decimal("0.00"))
        portfolio_avg_margin_pct = (
            (total_priced_profit / total_priced_revenue * Decimal("100")).quantize(Decimal("0.1"))
            if total_priced_revenue > 0
            else Decimal("0.0")
        )

        return render(
            request,
            self.template_name,
            {
                "products": display_products,
                "priced_count": len(priced_list),
                "unpriced_count": len(unpriced_list),
                "margin_traps_count": len(margin_traps),
                "margin_traps": margin_traps[:4],
                "top_sku": top_sku,
                "portfolio_avg_margin_pct": portfolio_avg_margin_pct,
                "total_priced_profit": total_priced_profit,
                "tier_filter": tier_filter,
            },
        )
