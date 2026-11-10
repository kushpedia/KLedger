from django.urls import path
from apps.reports.views import ReportStudioCenterView

app_name = "reports"

urlpatterns = [
    path("", ReportStudioCenterView.as_view(), name="report_center"),
]
