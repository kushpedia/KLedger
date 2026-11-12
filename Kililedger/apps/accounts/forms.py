"""
Forms for Multi-Tenant Authentication, Vendor Registration, Staff Invitations,
and Reconciliation Rules Configuration (`apps.accounts`).
"""
from django import forms
from django.contrib.auth import authenticate
from django.db import transaction

from apps.accounts.models import VendorTenant, VendorUser

INPUT_CLASSES = (
    "w-full rounded-lg border border-slate-300 bg-white px-3.5 py-2.5 text-xs text-slate-900 "
    "placeholder:text-slate-400 focus:border-[#0F766E] focus:outline-none focus:ring-2 focus:ring-[#0F766E]/20"
)


class VendorRegistrationForm(forms.Form):
    """
    Creates both an isolated `VendorTenant` and the primary `VendorUser (ROLE_OWNER)`
    inside an atomic database transaction (SCR-01).
    Supports `is_active=False` for email verification activation workflows.
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
    def save(self, is_active: bool = False, commit: bool = True) -> VendorUser:
        email = self.cleaned_data["email"]
        tenant = VendorTenant.objects.create(
            business_name=self.cleaned_data["business_name"].strip(),
            contact_email=email,
            contact_phone=self.cleaned_data["phone_number"].strip(),
            default_accounting_basis="CASH_FLOW",
            pending_aging_threshold_days=14,
            critical_aging_threshold_days=21,
            is_active=is_active,
        )
        user = VendorUser.objects.create_user(
            username=email,
            email=email,
            password=self.cleaned_data["password"],
            tenant=tenant,
            role=VendorUser.ROLE_OWNER,
            phone_number=self.cleaned_data["phone_number"].strip(),
            first_name=self.cleaned_data["business_name"].strip()[:30],
            is_active=is_active,
        )
        return user


class VendorLoginForm(forms.Form):
    """Authenticates a VendorUser or Platform Admin by email/username and password (SCR-01)."""

    email = forms.CharField(
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "seller@kushpedia.co.ke"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASSES, "placeholder": "Your account password"}),
    )

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean(self):
        email_or_user = (self.cleaned_data.get("email") or "").strip().lower()
        password = self.cleaned_data.get("password")

        if email_or_user and password:
            user = authenticate(self.request, username=email_or_user, password=password)
            if user is None:
                user_match = VendorUser.objects.filter(email__iexact=email_or_user).first()
                if user_match:
                    user = authenticate(self.request, username=user_match.username, password=password)

            if user is None:
                raise forms.ValidationError("Invalid email or password. Please verify your credentials.")
            self.user_cache = user
        return self.cleaned_data

    def get_user(self):
        return self.user_cache


class StaffMemberInviteForm(forms.Form):
    """Invites a sub-account user (Finance Auditor or Ops Assistant) to a tenant workspace (SCR-15)."""

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": INPUT_CLASSES, "placeholder": "accountant@kushpedia.co.ke"}),
    )
    full_name = forms.CharField(
        max_length=120,
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "Jane Mwangi"}),
    )
    role = forms.ChoiceField(
        choices=(
            (VendorUser.ROLE_FINANCE, "Finance & Reconciliation Auditor (Full Financials, Read/Write COGS)"),
            (VendorUser.ROLE_OPERATIONS, "Operations Assistant (Shipment Tracking & Returns, Zero Financials)"),
        ),
        widget=forms.Select(attrs={"class": INPUT_CLASSES}),
    )
    temporary_password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASSES, "placeholder": "Temporary Password"}),
    )


class ReconciliationRulesForm(forms.ModelForm):
    """Configures store reconciliation and threshold preferences (SCR-15)."""

    class Meta:
        model = VendorTenant
        fields = (
            "default_accounting_basis",
            "cogs_update_policy",
            "target_net_margin_pct",
            "pending_aging_threshold_days",
            "critical_aging_threshold_days",
        )
        widgets = {
            "default_accounting_basis": forms.Select(attrs={"class": INPUT_CLASSES}),
            "cogs_update_policy": forms.Select(attrs={"class": INPUT_CLASSES}),
            "target_net_margin_pct": forms.NumberInput(attrs={"class": INPUT_CLASSES, "step": "0.1"}),
            "pending_aging_threshold_days": forms.NumberInput(attrs={"class": INPUT_CLASSES}),
            "critical_aging_threshold_days": forms.NumberInput(attrs={"class": INPUT_CLASSES}),
        }
