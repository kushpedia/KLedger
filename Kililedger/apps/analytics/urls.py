from django.urls import path
from apps.analytics.views import (
    ChartDataJsonView,
    DashboardOverviewView,
    FinancialAnalyticsPnlView,
)

app_name = "analytics"

urlpatterns = [
    path("", DashboardOverviewView.as_view(), name="dashboard"),
    path("pnl/", FinancialAnalyticsPnlView.as_view(), name="pnl_statement"),
    path("chart-data/", ChartDataJsonView.as_view(), name="chart_data_json"),
]
