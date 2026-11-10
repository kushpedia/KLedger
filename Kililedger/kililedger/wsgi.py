"""WSGI config for KiliLedger Pro."""
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kililedger.settings.production")
application = get_wsgi_application()
