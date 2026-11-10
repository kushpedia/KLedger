from django.urls import path
from apps.products.views import (
    BulkCogsCsvImportExportView,
    InlineCogsUpdatePartialView,
    ProductCatalogueView,
    ProductCostHistoryDrawerView,
    ProductProfitabilityLeaderboardView,
)

app_name = "products"

urlpatterns = [
    path("", ProductCatalogueView.as_view(), name="product_catalogue"),
    path("profitability/", ProductProfitabilityLeaderboardView.as_view(), name="profitability_rank"),
    path("bulk-cogs-csv/", BulkCogsCsvImportExportView.as_view(), name="bulk_cogs_csv"),
    path("<uuid:pk>/update-cogs/", InlineCogsUpdatePartialView.as_view(), name="update_cogs"),
    path("<uuid:pk>/history-drawer/", ProductCostHistoryDrawerView.as_view(), name="cost_history_drawer"),
]
