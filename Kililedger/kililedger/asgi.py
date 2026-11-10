"""ASGI config for KiliLedger Pro."""
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kililedger.settings.production")
application = get_asgi_application()
