"""
Views for New Vendor Onboarding Wizard (SCR-02) and Store Management Directory & Alias Config (SCR-04).
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.accounts.middleware import VendorRequiredMixin
from apps.accounts.models import VendorUser
from apps.stores.forms import StoreAliasAddForm, StoreRegistrationForm
from apps.stores.models import Store, StoreAlias
from services.parser import KilimallWorkbookParser


class OnboardingWizardView(VendorRequiredMixin, View):
    """
    Implements SCR-02: 3-Step First Store & First Statement Setup Wizard.
    Supports both Manual Store Registration and 1-Click Auto-Detection from a dropped Kilimall `.xlsx` bill.
    """

    template_name = "stores/onboarding_wizard.html"
    allowed_roles = (VendorUser.ROLE_OWNER, VendorUser.ROLE_FINANCE)

    def get(self, request):
        form = StoreRegistrationForm(tenant=request.tenant)
        existing_stores = Store.objects.for_tenant(request.tenant)
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "existing_stores": existing_stores,
            },
        )

    def post(self, request):
        mode = request.POST.get("onboarding_mode", "manual")

        # Fast-Track Auto-Setup from dropped Excel file
        if mode == "auto_excel" and request.FILES.get("excel_file"):
            excel_file = request.FILES["excel_file"]
            parser = KilimallWorkbookParser()
            parsed = parser.parse_file(excel_file, filename=excel_file.name)

            if not parsed.is_valid or not parsed.kilimall_store_id:
                err_msg = "; ".join(parsed.errors) if parsed.errors else "Could not detect Store ID from workbook."
                messages.error(request, f"Excel Auto-Setup failed: {err_msg}")
                return redirect("stores:onboarding_wizard")

            # Verify Store ID isn't claimed by a different vendor tenant
            claimed_other = (
                Store.objects.filter(kilimall_store_id=parsed.kilimall_store_id)
                .exclude(tenant=request.tenant)
                .exists()
            )
            if claimed_other:
                messages.error(
                    request,
                    f"Store ID {parsed.kilimall_store_id} is already registered to another vendor account.",
                )
                return redirect("stores:onboarding_wizard")

            store, created = Store.objects.get_or_create(
                tenant=request.tenant,
                kilimall_store_id=parsed.kilimall_store_id,
                defaults={
                    "store_name": parsed.primary_store_name or f"Kilimall Store {parsed.kilimall_store_id}",
                    "fulfillment_type": Store.FULFILLMENT_DS,
                },
            )
            for alias_item in parsed.discovered_aliases:
                store.register_alias(alias_item["alias_name"], alias_item["source_sheet"])

            # Save parsed payload in session so user lands directly on SCR-05 Step 2 (Validation & Preview)
            request.session["pending_statement_preview"] = parsed.to_dict()
            request.session["active_store_scope"] = store.kilimall_store_id
            messages.success(
                request,
                f"Auto-detected Store '{store.store_name}' (ID: {store.kilimall_store_id}) and validated '{excel_file.name}'! Review your statement impact below.",
            )
            return redirect("statements:upload_wizard")

        # Manual Store Registration
        form = StoreRegistrationForm(tenant=request.tenant, data=request.POST)
        if form.is_valid():
            store = form.save()
            request.session["active_store_scope"] = store.kilimall_store_id
            messages.success(
                request,
                f"Store '{store.store_name}' (ID: {store.kilimall_store_id}) registered! Now upload your first Kilimall settlement Excel.",
            )
            return redirect("statements:upload_wizard")

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "existing_stores": Store.objects.for_tenant(request.tenant),
            },
        )


class StoreDirectoryView(VendorRequiredMixin, View):
    """
    Implements SCR-04: Store Management Directory & Workbook Alias Configuration.
    """

    template_name = "stores/store_directory.html"

    def get(self, request):
        stores = Store.objects.for_tenant(request.tenant).prefetch_related("aliases", "statements")
        all_aliases = StoreAlias.objects.filter(store__tenant=request.tenant).select_related("store")
        form = StoreRegistrationForm(tenant=request.tenant)
        alias_form = StoreAliasAddForm()
        return render(
            request,
            self.template_name,
            {
                "stores": stores,
                "all_aliases": all_aliases,
                "form": form,
                "alias_form": alias_form,
            },
        )

    def post(self, request):
        if not request.user.can_manage_stores_and_rollback:
            messages.error(request, "Only the Vendor Owner can add or modify store registrations.")
            return redirect("stores:store_directory")

        action = request.POST.get("store_action", "create_store")

        if action == "add_alias":
            store_id = request.POST.get("store_id")
            store = get_object_or_404(Store, id=store_id, tenant=request.tenant)
            alias_form = StoreAliasAddForm(request.POST)
            if alias_form.is_valid():
                store.register_alias(
                    alias_form.cleaned_data["alias_name"],
                    alias_form.cleaned_data["source_sheet"],
                )
                messages.success(
                    request,
                    f"Workbook alias '{alias_form.cleaned_data['alias_name']}' linked to {store.store_name} (ID: {store.kilimall_store_id}).",
                )
            return redirect("stores:store_directory")

        if action == "toggle_active":
            store_id = request.POST.get("store_id")
            store = get_object_or_404(Store, id=store_id, tenant=request.tenant)
            store.is_active = not store.is_active
            store.save(update_fields=["is_active", "updated_at"])
            state_str = "activated" if store.is_active else "archived"
            messages.info(request, f"Store '{store.store_name}' {state_str}.")
            return redirect("stores:store_directory")

        # Default: Create new store
        form = StoreRegistrationForm(tenant=request.tenant, data=request.POST)
        if form.is_valid():
            store = form.save()
            messages.success(request, f"Registered new Kilimall store: {store.store_name} (ID: {store.kilimall_store_id}).")
            return redirect("stores:store_directory")

        stores = Store.objects.for_tenant(request.tenant).prefetch_related("aliases", "statements")
        all_aliases = StoreAlias.objects.filter(store__tenant=request.tenant).select_related("store")
        return render(
            request,
            self.template_name,
            {
                "stores": stores,
                "all_aliases": all_aliases,
                "form": form,
                "alias_form": StoreAliasAddForm(),
            },
        )
