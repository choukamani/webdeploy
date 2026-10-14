"""System alerts API — GCP health monitoring, quota alerts, custom alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.database import get_db
from db import crud
from api.dependencies import require_admin

logger = logging.getLogger("webdeploy.api.alerts")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertResponse(BaseModel):
    id: str
    severity: str  # "info", "warning", "error", "critical"
    source: str    # "system", "quota", "gcp", "manual"
    title: str
    message: str
    resolved: bool = False
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


class AlertCreate(BaseModel):
    severity: str = "warning"
    source: str = "manual"
    title: str
    message: str


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/alerts — list all alerts
# ═══════════════════════════════════════════════════════════════════════

@router.get("", response_model=list[AlertResponse])
def list_alerts(
    resolved: Optional[bool] = None,
    limit: int = 50,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """List system alerts (admin only)."""
    alerts = crud.list_alerts(db, resolved=resolved, limit=limit)
    return [
        AlertResponse(
            id=a.id,
            severity=getattr(a, "severity", "info"),
            source=getattr(a, "source", "system"),
            title=getattr(a, "title", ""),
            message=getattr(a, "message", ""),
            resolved=getattr(a, "resolved", False),
            created_at=getattr(a, "created_at", None),
            resolved_at=getattr(a, "resolved_at", None),
        )
        for a in alerts
    ]


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/alerts — create manual alert
# ═══════════════════════════════════════════════════════════════════════

@router.post("", response_model=AlertResponse, status_code=201)
def create_alert(
    body: AlertCreate,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """Create a manual alert (admin only)."""
    alert = crud.create_alert(
        db,
        severity=body.severity,
        source=body.source,
        title=body.title,
        message=body.message,
    )
    return AlertResponse(
        id=alert.id,
        severity=alert.severity,
        source=alert.source,
        title=alert.title,
        message=alert.message,
        resolved=False,
        created_at=alert.created_at,
    )


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/alerts/{id}/resolve — resolve an alert
# ═══════════════════════════════════════════════════════════════════════

@router.post("/{alert_id}/resolve")
def resolve_alert(
    alert_id: str,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """Mark an alert as resolved."""
    crud.resolve_alert(db, alert_id)
    return {"resolved": True, "alert_id": alert_id}


# ═══════════════════════════════════════════════════════════════════════
#  DELETE /api/alerts/{id} — delete an alert
# ═══════════════════════════════════════════════════════════════════════

@router.delete("/{alert_id}")
def delete_alert(
    alert_id: str,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """Delete an alert."""
    crud.delete_alert(db, alert_id)
    return {"deleted": True, "alert_id": alert_id}


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/alerts/unresolved-count — quick badge count
# ═══════════════════════════════════════════════════════════════════════

@router.get("/unresolved-count")
def unresolved_count(user=Depends(require_admin), db=Depends(get_db)):
    """Return count of unresolved alerts (for nav badge)."""
    count = crud.count_unresolved_alerts(db)
    return {"count": count}
