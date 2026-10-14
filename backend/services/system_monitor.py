"""System monitor — background task that checks GCP service health and creates alerts."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config import get_settings
from db.database import SessionLocal

logger = logging.getLogger("webdeploy.system_monitor")

# Check interval: every 5 minutes
_CHECK_INTERVAL = 300


async def system_monitor_task() -> None:
    """Background task that periodically checks system health and creates alerts."""
    # Wait a bit after startup before first check
    await asyncio.sleep(30)

    while True:
        try:
            await _run_health_checks()
        except Exception as exc:
            logger.error("System monitor error: %s", exc)

        await asyncio.sleep(_CHECK_INTERVAL)


async def _run_health_checks() -> None:
    """Run all health checks and create/resolve alerts."""
    settings = get_settings()
    db = SessionLocal()

    # 1. Check for stale deployments (running > 20 min)
    now = datetime.now(timezone.utc)
    running_query = db.collection("deployments").where("status", "==", "running")
    for doc in running_query.stream():
        data = doc.to_dict()
        started = data.get("started_at")
        if started and hasattr(started, "timestamp"):
            elapsed = (now - started.replace(tzinfo=timezone.utc)).total_seconds()
            if elapsed > 1200:  # 20 minutes
                _create_alert_if_new(
                    db,
                    source="system",
                    severity="warning",
                    title=f"Deploiement bloque: {data.get('website_name', doc.id[:8])}",
                    message=f"Le deploiement {doc.id[:8]} est en cours depuis {int(elapsed/60)} minutes.",
                    unique_key=f"stale-{doc.id}",
                )

    # 2. Check deployment failure rate (last hour)
    from datetime import timedelta
    hour_ago = now - timedelta(hours=1)
    recent_query = db.collection("deployments").where("created_at", ">=", hour_ago)
    recent_docs = list(recent_query.stream())
    if len(recent_docs) >= 3:
        failed = sum(1 for d in recent_docs if d.to_dict().get("status") == "failed")
        rate = failed / len(recent_docs)
        if rate > 0.5:
            _create_alert_if_new(
                db,
                source="system",
                severity="error",
                title="Taux d'echec eleve",
                message=f"{failed}/{len(recent_docs)} deploiements echoues dans la derniere heure ({rate*100:.0f}%).",
                unique_key=f"failure-rate-{now.strftime('%Y-%m-%d-%H')}",
            )

    # 3. Check disk usage (queue too long)
    queued_query = db.collection("deployments").where("status", "==", "queued")
    queued_count = len(list(queued_query.stream()))
    if queued_count > 10:
        _create_alert_if_new(
            db,
            source="system",
            severity="warning",
            title="File d'attente longue",
            message=f"{queued_count} deploiements en attente dans la file.",
            unique_key=f"queue-long-{now.strftime('%Y-%m-%d-%H')}",
        )

    # 4. Check pending user approvals
    pending_query = db.collection("users").where("status", "==", "pending")
    pending_count = len(list(pending_query.stream()))
    if pending_count > 0:
        _create_alert_if_new(
            db,
            source="system",
            severity="info",
            title=f"{pending_count} utilisateur(s) en attente",
            message=f"Il y a {pending_count} demande(s) d'acces en attente d'approbation.",
            unique_key=f"pending-users-{now.strftime('%Y-%m-%d')}",
        )


def _create_alert_if_new(db, *, source: str, severity: str, title: str, message: str, unique_key: str) -> None:
    """Create an alert only if one with the same unique_key doesn't already exist (unresolved).

    Uses a single-field query + Python filter to avoid composite index requirements.
    """
    try:
        # Query by unique_key only (single field), then filter resolved in Python
        existing = (
            db.collection("system_alerts")
            .where("unique_key", "==", unique_key)
        )
        for doc in existing.stream():
            data = doc.to_dict()
            if data.get("resolved") is False:
                return  # Already exists and unresolved

        db.collection("system_alerts").add({
            "severity": severity,
            "source": source,
            "title": title,
            "message": message,
            "resolved": False,
            "unique_key": unique_key,
            "created_at": datetime.now(timezone.utc),
            "resolved_at": None,
        })
        logger.info("Alert created: [%s] %s", severity, title)
    except Exception as exc:
        logger.warning("Failed to create alert: %s", exc)
