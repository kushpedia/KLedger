from django.contrib import admin
from apps.reports.models import GeneratedReportArchive


@admin.register(GeneratedReportArchive)
class GeneratedReportArchiveAdmin(admin.ModelAdmin):
    list_display = ("report_title", "export_format", "store_scope_label", "accounting_basis", "generated_by", "created_at")
    list_filter = ("export_format", "report_code", "accounting_basis")
