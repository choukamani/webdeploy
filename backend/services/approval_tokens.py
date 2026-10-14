"""HMAC-based approval tokens for email-based user approval/rejection.

Zero extra dependencies — uses stdlib ``hmac`` and ``hashlib``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

from config import get_settings

logger = logging.getLogger("webdeploy.approval_tokens")

_TOKEN_EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days

# Used only when APPROVAL_TOKEN_SECRET is not configured. Random per process, so
# tokens cannot be forged; approval links simply stop working after a restart.
_EPHEMERAL_SECRET = secrets.token_hex(32)


def _get_secret() -> str:
    secret = get_settings().APPROVAL_TOKEN_SECRET
    if not secret:
        logger.warning(
            "APPROVAL_TOKEN_SECRET is not set: using a random per-process secret; "
            "approval links will be invalid after a restart"
        )
        return _EPHEMERAL_SECRET
    return secret


def generate_approval_token(uid: str, action: str) -> str:
    """Generate an HMAC-SHA256 token encoding uid + action + timestamp."""
    ts = str(int(time.time()))
    payload = f"{uid}:{action}:{ts}"
    sig = hmac.new(
        _get_secret().encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    return f"{ts}.{sig}"


def verify_approval_token(uid: str, action: str, token: str) -> bool:
    """Verify an approval token — checks signature and expiry (7 days)."""
    try:
        ts_str, sig = token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False

    # Check expiry
    if time.time() - ts > _TOKEN_EXPIRY_SECONDS:
        return False

    # Recompute expected signature
    payload = f"{uid}:{action}:{ts_str}"
    expected = hmac.new(
        _get_secret().encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(sig, expected)
