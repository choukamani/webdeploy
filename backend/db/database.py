"""Firestore database client and session management.

Replaces the previous SQLAlchemy/SQLite implementation with Google Cloud
Firestore for scalable, serverless persistence.
"""

from __future__ import annotations

import logging
import os

from google.cloud import firestore

from config import get_settings
from infra.gcp_helpers import get_credentials

logger = logging.getLogger("webdeploy.database")

_settings = get_settings()

# ── Firestore client (module-level singleton) ────────────────────────
_credentials = get_credentials(_settings.GOOGLE_APPLICATION_CREDENTIALS)
_firestore_client = firestore.Client(
    project=_settings.PROJECT_ID,
    credentials=_credentials,
)


def init_db() -> None:
    """No-op for Firestore — collections are created on first write."""
    logger.info("Firestore client initialised (project: %s)", _settings.PROJECT_ID)


def get_db():
    """Yield the Firestore client — drop-in replacement for FastAPI Depends."""
    yield _firestore_client


# Alias used by the pipeline orchestrator (non-dependency-injected context)
def SessionLocal():
    """Return the Firestore client directly (replaces SQLAlchemy SessionLocal)."""
    return _firestore_client
