"""
Outbound Email Dispatch Service (`services/email_service.py`).

Provides branded, multi-part (HTML + plain text fallback) transactional emails:
- Account creation & email verification links (`send_vendor_activation_email`)
- Password resets & tenant alerts
"""
from __future__ import annotations

import logging
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from apps.accounts.tokens import generate_activation_token

logger = logging.getLogger(__name__)


def send_vendor_activation_email(user, request=None, domain: str | None = None) -> bool:
    """
    Dispatches a branded HTML email containing a secure 48-hour activation link.
    Works in development (console backend) and production (SMTP/SES/SendGrid).
    """
    token = generate_activation_token(user)

    if request:
        activation_url = request.build_absolute_uri(f"/activate/{token}/")
    else:
        base_domain = domain or getattr(settings, "PRIMARY_DOMAIN", "http://127.0.0.1:8000")
        activation_url = f"{base_domain.rstrip('/')}/activate/{token}/"

    tenant_name = getattr(user.tenant, "business_name", "Your Business") if getattr(user, "tenant", None) else "Your Business"

    context = {
        "user": user,
        "business_name": tenant_name,
        "activation_url": activation_url,
        "expires_hours": 48,
    }

    subject = f"Verify your KiliLedger Pro account — {tenant_name}"
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "KiliLedger Pro <support@kililedger.co.ke>")
    to_email = user.email

    html_content = render_to_string("emails/account_activation.html", context)
    text_content = strip_tags(html_content)

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, [to_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        return True
    except Exception as e:
        logger.error(f"Failed to send activation email to {to_email}: {e}")
        return False
