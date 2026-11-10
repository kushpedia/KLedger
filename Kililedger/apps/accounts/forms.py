"""
Forms for Vendor Registration, Sign In, Reconciliation Rules (SCR-15), and Team Management.
"""
from django import forms
from django.contrib.auth import authenticate
from django.db import transaction
from apps.accounts.models import VendorTenant, VendorUser


INPUT_CLASSES = (
    "w-full rounded-md border border-slate-300 bg-white px-3.5 py-2 text-sm text-slate-900 "
    "placeholder-slate-400 shadow-sm focus:border-teal-600 focus:outline-none focus:ring-2 focus:ring-teal-600/20"
)


class VendorRegistrationForm(forms.Form):
    """
    Creates both an isolated `VendorTenant` and the primary `VendorUser (ROLE_OWNER)`
    inside an atomic database transaction (SCR-01).
    """

    business_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "Kushpedia Enterprises"}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": INPUT_CLASSES, "placeholder": "seller@kushpedia.co.ke"}),
    )
    phone_number = forms.CharField(
        max_length=25,
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "+254 712 345 678"}),
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASSES, "placeholder": "Minimum 8 characters"}),
    )
    accept_terms = forms.BooleanField(
        required=True,
        error_messages={"required": "You must accept the Tenant Data Isolation & Terms of Service."},
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if VendorUser.objects.filter(email__iexact=email).exists() or VendorTenant.objects.filter(contact_email__iexact=email).exists():
            raise forms.ValidationError("A vendor workspace with this email address already exists.")
        return email

    @transaction.atomic
    def save(self) -> VendorUser:
        email = self.cleaned_data["email"]
        tenant = VendorTenant.objects.create(
            business_name=self.cleaned_data["business_name"].strip(),
            contact_email=email,
            contact_phone=self.cleaned_data["phone_number"].strip(),
            default_accounting_basis="CASH_FLOW",
            pending_aging_threshold_days=14,
            critical_aging_threshold_days=21,
        )
        user = VendorUser.objects.create_user(
            username=email,
            email=email,
            password=self.cleaned_data["password"],
            tenant=tenant,
            role=VendorUser.ROLE_OWNER,
            phone_number=self.cleaned_data["phone_number"].strip(),
            first_name=self.cleaned_data["business_name"].strip()[:30],
        )
        return user


class VendorLoginForm(forms.Form):
    """Authenticates a VendorUser or Platform Admin by email/username and password (SCR-01)."""

    email = forms.CharField(
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "seller@kushpedia.co.ke"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASSES, "placeholder": "••••••••"}),
    )

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        identifier = cleaned_data.get("email", "").strip().lower()
        password = cleaned_data.get("password")

        if identifier and password:
            # Allow login by email or username
            user_obj = VendorUser.objects.filter(email__iexact=identifier).first()
            username = user_obj.username if user_obj else identifier
            self.user_cache = authenticate(self.request, username=username, password=password)
            if self.user_cache is None:
                raise forms.ValidationError("Invalid email or password. Please verify your credentials.")
            if not self.user_cache.is_active:
                raise forms.ValidationError("This user account has been suspended.")
        return cleaned_data

    def get_user(self):
        return self.user_cache


class ReconciliationRulesForm(forms.ModelForm):
    """
    Allows Vendor Owners to update their reconciliation aging thresholds,
    default accounting basis, and package size anomaly guards (SCR-15).
    """

    class Meta:
        model = VendorTenant
        fields = [
            "business_name",
            "contact_phone",
            "default_accounting_basis",
            "pending_aging_threshold_days",
            "critical_aging_threshold_days",
            "target_net_margin_pct",
            "flag_package_size_anomalies",
            "cogs_update_policy",
        ]
        widgets = {
            "business_name": forms.TextInput(attrs={"class": INPUT_CLASSES}),
            "contact_phone": forms.TextInput(attrs={"class": INPUT_CLASSES}),
            "default_accounting_basis": forms.Select(attrs={"class": INPUT_CLASSES}),
            "pending_aging_threshold_days": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": 3, "max": 60}),
            "critical_aging_threshold_days": forms.NumberInput(attrs={"class": INPUT_CLASSES, "min": 7, "max": 90}),
            "target_net_margin_pct": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.5"}),
            "cogs_update_policy": forms.Select(attrs={"class": INPUT_CLASSES}),
        }

    def clean(self):
        cleaned = super().clean()
        pending_days = cleaned.get("pending_aging_threshold_days") or 14
        critical_days = cleaned.get("critical_aging_threshold_days") or 21
        if critical_days <= pending_days:
            self.add_error(
                "critical_aging_threshold_days",
                "Critical escalation window must be greater than the initial Overdue Pending threshold.",
            )
        return cleaned


class StaffMemberInviteForm(forms.Form):
    """Allows Vendor Owners to create sub-accounts for Finance Managers or Operations Assistants (SCR-15)."""

    full_name = forms.CharField(max_length=100, widget=forms.TextInput(attrs={"class": INPUT_CLASSES}))
    email = forms.EmailField(widget=forms.EmailInput(attrs={"class": INPUT_CLASSES}))
    role = forms.ChoiceField(
        choices=[
            (VendorUser.ROLE_FINANCE, "Finance Manager (Statements, COGS & Reports)"),
            (VendorUser.ROLE_OPERATIONS, "Operations Assistant (Pending Orders & Penalties Only)"),
        ],
        widget=forms.Select(attrs={"class": INPUT_CLASSES}),
    )
    temporary_password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASSES}),
    )
