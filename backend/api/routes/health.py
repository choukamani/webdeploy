"""
Health-check route — lightweight endpoint for load balancers and monitoring.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])

_VERSION = "1.0.0"


@router.get("/health")
def health_check() -> dict:
    """Return a simple health-check payload with server timestamp."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": _VERSION,
    }
