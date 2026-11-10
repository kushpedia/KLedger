"""
Forms for Inline HTMX Buying Price (COGS) Entry and Bulk CSV Import (`apps.products`).
"""
from decimal import Decimal, InvalidOperation
from django import forms


class InlineBuyingPriceForm(forms.Form):
    """Validates a single inline HTMX buying price update (`SCR-11`)."""

    buying_price = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
        error_messages={
            "required": "Enter a valid buying price.",
            "min_value": "Buying price must be greater than KES 0.00.",
            "invalid": "Enter a numeric KES amount.",
        },
    )
    supplier_note = forms.CharField(required=False, max_length=200)


class BulkCogsCsvUploadForm(forms.Form):
    """Validates a CSV file containing `goods_id,buying_price,supplier_note` rows (`SCR-11`)."""

    csv_file = forms.FileField(
        help_text="Upload a CSV file with columns: goods_id, buying_price, supplier_note (optional)."
    )

    def clean_csv_file(self):
        f = self.cleaned_data["csv_file"]
        if not f.name.lower().endswith(".csv"):
            raise forms.ValidationError("Please upload a valid .csv file.")
        return f
