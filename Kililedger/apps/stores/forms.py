"""
Forms for Registering Kilimall Stores, Adding Workbook Aliases, and Auto-Detecting Stores from Excel (SCR-02 & SCR-04).
"""
from django import forms
from apps.accounts.forms import INPUT_CLASSES
from apps.stores.models import Store


class StoreRegistrationForm(forms.ModelForm):
    """
    Registers a Kilimall store under the logged-in vendor's tenant and optionally seeds
    known secondary aliases (such as 'Harambee Ⅰ').
    """

    secondary_alias = forms.CharField(
        required=False,
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASSES,
                "placeholder": "Optional: e.g. Harambee Ⅰ (used on fine sheet)",
            }
        ),
        help_text="If Kilimall uses an older or secondary profile name on penalty tabs, enter it here.",
    )

    class Meta:
        model = Store
        fields = ["store_name", "kilimall_store_id", "fulfillment_type"]
        widgets = {
            "store_name": forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "Kushpedia Stores4"}),
            "kilimall_store_id": forms.TextInput(
                attrs={"class": f"{INPUT_CLASSES} font-mono", "placeholder": "10344"}
            ),
            "fulfillment_type": forms.Select(attrs={"class": INPUT_CLASSES}),
        }

    def __init__(self, tenant=None, *args, **kwargs):
        self.tenant = tenant
        super().__init__(*args, **kwargs)

    def clean_kilimall_store_id(self):
        store_id = str(self.cleaned_data["kilimall_store_id"]).strip()
        if store_id.endswith(".0"):
            store_id = store_id[:-2]
        if not store_id:
            raise forms.ValidationError("Please provide a valid Kilimall Store ID.")

        # Enforce global uniqueness of a Kilimall Store ID across tenants (Store Claiming Protection)
        existing = Store.objects.filter(kilimall_store_id=store_id).exclude(pk=self.instance.pk).first()
        if existing:
            if self.tenant and existing.tenant_id == self.tenant.id:
                raise forms.ValidationError(f"Store ID {store_id} ({existing.store_name}) is already registered in your workspace.")
            raise forms.ValidationError(
                f"Store ID {store_id} is already claimed by another registered workspace. Contact support if you own this store."
            )
        return store_id

    def save(self, commit=True):
        store = super().save(commit=False)
        if self.tenant:
            store.tenant = self.tenant
        if commit:
            store.save()
            store.register_alias(store.store_name, source_sheet="bill")
            sec_alias = self.cleaned_data.get("secondary_alias", "").strip()
            if sec_alias:
                store.register_alias(sec_alias, source_sheet="fine")
        return store


class StoreAliasAddForm(forms.Form):
    """Adds a manual store name alias to an existing Store (SCR-04)."""

    alias_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "e.g. Harambee Ⅰ"}),
    )
    source_sheet = forms.CharField(
        max_length=50,
        initial="fine",
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "fine"}),
    )
