"""Admin dashboard API — real-time stats, queue info, service health."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends

from config import Settings, get_settings
from db.database import get_db
from api.dependencies import require_admin

logger = logging.getLogger("webdeploy.api.dashboard")

router = APIRouter(prefix="/api/admin/dashboard", tags=["admin-dashboard"])


@router.get("")
async def get_dashboard(
    user=Depends(require_admin),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Return real-time dashboard data for admins."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── Active deployments (running) ──────────────────────────────────
    active_deployments = []
    try:
        running_query = db.collection("deployments").where("status", "==", "running")
        for doc in running_query.stream():
            data = doc.to_dict()
            started = data.get("started_at")
            elapsed = 0
            if started and hasattr(started, "timestamp"):
                elapsed = int((now - started.replace(tzinfo=timezone.utc)).total_seconds())
            active_deployments.append({
                "id": doc.id,
                "website_name": data.get("website_name", ""),
                "mode": data.get("mode", ""),
                "current_step": data.get("current_step", ""),
                "deployer_email": data.get("deployer_email", ""),
                "elapsed_seconds": elapsed,
            })
    except Exception as exc:
        logger.warning("Dashboard: failed to fetch active deployments: %s", exc)

    # ── Queue size ────────────────────────────────────────────────────
    queue_size = 0
    try:
        queued_query = db.collection("deployments").where("status", "==", "queued")
        queue_size = len(list(queued_query.stream()))
    except Exception as exc:
        logger.warning("Dashboard: failed to fetch queue: %s", exc)

    # ── Today's stats — use recent deployments and filter in Python ───
    today_total = 0
    today_success = 0
    today_failed = 0
    recent = []
    try:
        # Fetch recent deployments ordered by created_at (single field = no composite index)
        recent_query = (
            db.collection("deployments")
            .order_by("created_at", direction="DESCENDING")
            .limit(200)
        )
        recent_docs = list(recent_query.stream())

        for doc in recent_docs:
            data = doc.to_dict()
            created = data.get("created_at")

            # Build recent list (first 10)
            if len(recent) < 10:
                recent.append({
                    "id": doc.id,
                    "website_name": data.get("website_name", ""),
                    "mode": data.get("mode", ""),
                    "status": data.get("status", ""),
                    "deployer_email": data.get("deployer_email", ""),
                    "created_at": created,
                    "completed_at": data.get("completed_at"),
                })

            # Count today's stats
            if created and hasattr(created, "timestamp"):
                created_utc = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created
                if created_utc >= today_start:
                    today_total += 1
                    if data.get("status") == "success":
                        today_success += 1
                    elif data.get("status") == "failed":
                        today_failed += 1
    except Exception as exc:
        logger.warning("Dashboard: failed to fetch recent deployments: %s", exc)

    # ── User stats ────────────────────────────────────────────────────
    total_users = 0
    approved_users = 0
    pending_users = 0
    try:
        users_docs = list(db.collection("users").stream())
        total_users = len(users_docs)
        for d in users_docs:
            status = d.to_dict().get("status")
            if status == "approved":
                approved_users += 1
            elif status == "pending":
                pending_users += 1
    except Exception as exc:
        logger.warning("Dashboard: failed to fetch users: %s", exc)

    # ── Service health (non-blocking with timeout) ────────────────────
    services_health = await _check_services_health(settings)

    # ── 7-day trend (use the already-fetched recent docs) ─────────────
    week_ago = now - timedelta(days=7)
    daily_counts = {}
    try:
        week_query = (
            db.collection("deployments")
            .where("created_at", ">=", week_ago)
        )
        for doc in week_query.stream():
            data = doc.to_dict()
            created = data.get("created_at")
            if created and hasattr(created, "strftime"):
                day_key = created.strftime("%Y-%m-%d")
                daily_counts[day_key] = daily_counts.get(day_key, 0) + 1
    except Exception as exc:
        logger.warning("Dashboard: failed to fetch trend data: %s", exc)

    trend = []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        trend.append({"date": day, "count": daily_counts.get(day, 0)})

    return {
        "active_deployments": active_deployments,
        "queue_size": queue_size,
        "today": {
            "total": today_total,
            "success": today_success,
            "failed": today_failed,
        },
        "recent_deployments": recent,
        "users": {
            "total": total_users,
            "approved": approved_users,
            "pending": pending_users,
        },
        "services": services_health,
        "trend": trend,
    }


async def _check_services_health(settings: Settings) -> list[dict]:
    """Check health of key GCP services with timeouts to avoid blocking."""
    services = []

    # Firestore — already known to work if we got this far
    services.append({"name": "Firestore", "status": "healthy", "message": ""})

    # Gmail API — quick local check only (no network call)
    try:
        from services.email_service import EmailService
        svc = EmailService(settings=settings)
        configured = svc._is_gmail_configured()
        services.append({
            "name": "Gmail API",
            "status": "healthy" if configured else "warning",
            "message": "" if configured else "Non configure",
        })
    except Exception as exc:
        services.append({"name": "Gmail API", "status": "error", "message": str(exc)[:100]})

    # Cloud Storage — quick check with timeout
    async def _check_storage():
        try:
            from google.cloud import storage
            client = storage.Client(project=settings.PROJECT_ID)
            bucket = client.bucket(settings.deploy_uploads_bucket_name)
            await asyncio.to_thread(bucket.exists)
            return {"name": "Cloud Storage", "status": "healthy", "message": ""}
        except Exception as exc:
            return {"name": "Cloud Storage", "status": "warning", "message": str(exc)[:100]}

    try:
        result = await asyncio.wait_for(_check_storage(), timeout=5)
        services.append(result)
    except asyncio.TimeoutError:
        services.append({"name": "Cloud Storage", "status": "warning", "message": "Timeout"})

    # Cloud Run — quick check with timeout
    async def _check_cloudrun():
        try:
            from google.cloud import run_v2
            client = run_v2.ServicesClient()
            parent = f"projects/{settings.PROJECT_ID}/locations/{settings.CLOUDRUN_REGION}"
            await asyncio.to_thread(lambda: list(client.list_services(parent=parent, timeout=4)))
            return {"name": "Cloud Run", "status": "healthy", "message": ""}
        except Exception as exc:
            return {"name": "Cloud Run", "status": "warning", "message": str(exc)[:100]}

    try:
        result = await asyncio.wait_for(_check_cloudrun(), timeout=6)
        services.append(result)
    except asyncio.TimeoutError:
        services.append({"name": "Cloud Run", "status": "warning", "message": "Timeout"})

    return services
