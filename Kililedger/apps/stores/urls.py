from django.urls import path
from apps.stores.views import OnboardingWizardView, StoreDirectoryView

app_name = "stores"

urlpatterns = [
    path("", StoreDirectoryView.as_view(), name="store_directory"),
    path("onboarding/", OnboardingWizardView.as_view(), name="onboarding_wizard"),
]
