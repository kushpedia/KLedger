from django.urls import path
from apps.statements.views import (
    StatementDetailView,
    StatementLedgerView,
    StatementUploadWizardView,
)

app_name = "statements"

urlpatterns = [
    path("", StatementLedgerView.as_view(), name="statement_list"),
    path("upload/", StatementUploadWizardView.as_view(), name="upload_wizard"),
    path("<uuid:pk>/", StatementDetailView.as_view(), name="statement_detail"),
]
