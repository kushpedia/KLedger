"""
Cryptographically Signed Stateless Account Activation & Email Verification Tokens (`apps.accounts.tokens`).

Uses Django's built-in `TimestampSigner` with an application-specific cryptographic salt.
No temporary database tables or token cleanup cron jobs required.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

_ACTIVATION_SALT = "kililedger-pro-account-activation-salt"


def generate_activation_token(user) -> str:
    """
    Generates a cryptographically signed, timestamped token containing the user's UUID primary key.
    """
    signer = TimestampSigner(salt=_ACTIVATION_SALT)
    return signer.sign(str(user.pk))


def verify_activation_token(token: str, max_age_hours: int = 48):
    """
    Verifies token cryptographic integrity and expiration.
    Returns the matching User instance if valid and within the expiration window (default 48 hours),
    or None if the token is tampered with, malformed, or expired.
    """
    User = get_user_model()
    signer = TimestampSigner(salt=_ACTIVATION_SALT)
    try:
        max_age_seconds = max_age_hours * 3600
        user_pk = signer.unsign(token, max_age=max_age_seconds)
        return User.objects.filter(pk=user_pk).first()
    except (BadSignature, SignatureExpired):
        return None
