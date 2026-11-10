"""
Forms for Uploading and Validating Kilimall Excel Settlement Statements (`apps.statements`).
"""
from django import forms
from apps.accounts.forms import INPUT_CLASSES
from apps.stores.models import Store


class StatementExcelUploadForm(forms.Form):
    """
    Step 1 of SCR-05: Allows selecting a target store (or 'Auto-Detect from Excel')
    and uploading a Kilimall `.xlsx` settlement statement.
    """

    store = forms.ModelChoiceField(
        queryset=Store.objects.none(),
        required=False,
        empty_label="Auto-Detect Store ID from Excel ('bill' Sheet Row 2)",
        widget=forms.Select(attrs={"class": INPUT_CLASSES}),
    )
    excel_file = forms.FileField(
        widget=forms.ClearableFileInput(
            attrs={
                "class": "block w-full text-sm text-slate-600 file:mr-4 file:rounded-md file:border-0 file:bg-[#0F766E] file:px-4 file:py-2 file:text-xs file:font-semibold file:text-white hover:file:bg-[#115E59]",
                "accept": ".xlsx",
            }
        ),
        help_text="Upload your official Kilimall weekly settlement workbook (.xlsx, up to 10MB).",
    )

    def __init__(self, tenant=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if tenant:
            self.fields["store"].queryset = Store.objects.for_tenant(tenant).filter(is_active=True)

    def clean_excel_file(self):
        f = self.cleaned_data["excel_file"]
        if not f.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Invalid file format. Please upload a valid Microsoft Excel (.xlsx) workbook.")
        if f.size > 10 * 1024 * 1024:
            raise forms.ValidationError("File exceeds the 10MB maximum upload limit.")
        return f
