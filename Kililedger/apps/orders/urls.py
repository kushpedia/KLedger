from django.urls import path
from apps.orders.views import (
    FeePenaltyMonitoringCenterView,
    Order360DrawerPartialView,
    OrderReconciliationWorkspaceView,
    PendingShipmentsTrackerView,
)

app_name = "orders"

urlpatterns = [
    path("", OrderReconciliationWorkspaceView.as_view(), name="reconciliation_grid"),
    path("pending/", PendingShipmentsTrackerView.as_view(), name="pending_tracker"),
    path("fees-penalties/", FeePenaltyMonitoringCenterView.as_view(), name="fees_penalties_audit"),
    path("<str:order_sn>/drawer/", Order360DrawerPartialView.as_view(), name="order_360_drawer"),
]
