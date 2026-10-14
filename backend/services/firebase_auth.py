"""Firebase Admin SDK initialisation and token verification."""

from __future__ import annotations

import logging
import os

import firebase_admin
from firebase_admin import auth as firebase_auth, credentials as fb_credentials

logger = logging.getLogger("webdeploy.firebase_auth")

_app: firebase_admin.App | None = None


def init_firebase(settings=None) -> None:
    """Initialise the Firebase Admin SDK.

    Uses the service-account JSON pointed to by
    ``GOOGLE_APPLICATION_CREDENTIALS`` if available, otherwise falls back
    to Application Default Credentials (ADC) — which works out of the box
    on Cloud Run.
    """
    global _app
    if _app is not None:
        return

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Try settings first, then env var
    creds_path = ""
    if settings and getattr(settings, "GOOGLE_APPLICATION_CREDENTIALS", ""):
        creds_path = settings.GOOGLE_APPLICATION_CREDENTIALS
    if not creds_path:
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

    # Resolve relative path from the backend directory
    if creds_path and not os.path.isabs(creds_path):
        creds_path = os.path.join(backend_dir, creds_path)

    if creds_path and os.path.isfile(creds_path):
        cred = fb_credentials.Certificate(creds_path)
        _app = firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin initialised with service-account file: %s", creds_path)
    else:
        # ADC (Cloud Run, GCE, etc.)
        project_id = (
            (settings.PROJECT_ID if settings else None)
            or os.environ.get("PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        options = {"projectId": project_id} if project_id else {}
        _app = firebase_admin.initialize_app(options=options)
        logger.info("Firebase Admin initialised with ADC (project=%s).", project_id)


def verify_firebase_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return the decoded claims.

    Returns a dict with at least ``uid``, ``email``, and ``name`` keys.
    Raises ``ValueError`` on invalid / expired tokens.
    """
    if _app is None:
        init_firebase()

    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception as exc:
        raise ValueError(f"Invalid Firebase token: {exc}") from exc

    return {
        "uid": decoded["uid"],
        "email": decoded.get("email", ""),
        "name": decoded.get("name", ""),
        "picture": decoded.get("picture", ""),
    }
