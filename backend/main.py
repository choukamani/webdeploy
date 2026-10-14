"""
FastAPI application entry point for the WebDeploy platform.

Start the server with::

    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from db.database import init_db, SessionLocal
from models.enums import DeploymentMode, PipelineStep, StepStatus
from models.deployment import DeploymentConfig
from services.pipeline_orchestrator import PipelineOrchestrator
from services.zip_backup import restore_zip

# ── Route imports ─────────────────────────────────────────────────────
from api.routes.auth import router as auth_router
from api.routes.deployments import router as deployments_router
from api.routes.domains import router as domains_router
from api.routes.health import router as health_router
from api.routes.statistics import router as statistics_router
from api.routes.websocket import router as websocket_router
from api.routes.preview import router as preview_router
from api.routes.git import router as git_router
from api.routes.admin_dashboard import router as dashboard_router
from api.routes.quotas import router as quotas_router
from api.routes.alerts import router as alerts_router
from api.routes.websites import router as websites_router


# ── Logging configuration ────────────────────────────────────────────

def _configure_logging() -> None:
    """Set up Python logging based on the application settings."""
    settings = get_settings()
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Silence noisy third-party loggers
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("webdeploy")


# ── Stale deployment watchdog ─────────────────────────────────────────

_MAX_AUTO_RETRIES = 2
_QUEUED_GRACE_SECONDS = 300


def _recover_stale_deployments() -> list[dict]:
    """
    Find all deployments stuck in 'running' or 'queued' status and prepare
    them for automatic retry.

    Only recovers deployments that have been running longer than the pipeline
    timeout — this prevents a newly scaled Cloud Run instance from stealing
    a deployment that another instance is actively processing.

    If the ZIP file is still on disk and the deployment hasn't exceeded
    the retry limit, it will be reset to 'queued' and returned for
    re-execution.  Otherwise it is marked as failed with a friendly,
    non-technical message.

    Returns a list of dicts with the info needed to re-run the pipeline.
    """
    settings = get_settings()
    db = SessionLocal()
    to_retry: list[dict] = []
    now = datetime.now(timezone.utc)
    max_age = settings.PIPELINE_MAX_TIMEOUT_SECONDS

    for stale_status in ("running", "queued"):
        query = (
            db.collection("deployments")
            .where("status", "==", stale_status)
        )
        for doc in query.stream():
            data = doc.to_dict()
            deployment_id = doc.id
            retry_count = data.get("retry_count", 0)

            # ── Guard: skip deployments still actively running ─────────
            # Only recover deployments older than PIPELINE_MAX_TIMEOUT_SECONDS.
            # This prevents a new Cloud Run instance from stealing a deployment
            # that another instance is still processing.
            started_at = data.get("started_at")
            if stale_status == "running" and started_at is not None:
                if hasattr(started_at, "timestamp"):
                    elapsed = (now - started_at.replace(tzinfo=timezone.utc)).total_seconds()
                    if elapsed < max_age:
                        logger.info(
                            "Deployment %s still running (%ds < %ds timeout) — skipping recovery.",
                            deployment_id, int(elapsed), max_age,
                        )
                        continue

            # A freshly queued deployment belongs to the instance that queued
            # it (it starts within a second). Instances start whenever Cloud
            # Run scales out, so without this guard a new one would pick the
            # same deployment up and run it twice.
            created_at = data.get("created_at")
            if stale_status == "queued" and hasattr(created_at, "timestamp"):
                age = (now - created_at.replace(tzinfo=timezone.utc)).total_seconds()
                if age < _QUEUED_GRACE_SECONDS:
                    continue

            zip_path = settings.upload_path / f"{deployment_id}.zip"

            # If local ZIP is gone (ephemeral Cloud Run disk), restore from GCS
            if not zip_path.exists():
                try:
                    restore_zip(settings, deployment_id, zip_path)
                except Exception:
                    logger.warning("GCS restore failed for %s", deployment_id, exc_info=True)

            can_retry = retry_count < _MAX_AUTO_RETRIES and zip_path.exists()

            if can_retry:
                # ── Reset for automatic retry ─────────────────────────
                initial_steps = {step.value: StepStatus.PENDING.value for step in PipelineStep}
                doc.reference.update({
                    "status": "queued",
                    "current_step": None,
                    "steps_status": json.dumps(initial_steps),
                    "error_message": None,
                    "started_at": None,
                    "completed_at": None,
                    "retry_count": retry_count + 1,
                })

                to_retry.append({
                    "deployment_id": deployment_id,
                    "zip_path": str(zip_path),
                    "website_name": data.get("website_name", ""),
                    "mode": data.get("mode", "demo"),
                    "domain": data.get("domain"),
                    "notification_emails": data.get("notification_emails", ""),
                    "deployer_first_name": data.get("deployer_first_name", ""),
                    "deployer_last_name": data.get("deployer_last_name", ""),
                    "deployer_email": data.get("deployer_email", ""),
                    "ai_enabled": data.get("ai_enabled", False),
                })

                logger.info(
                    "Deployment %s queued for auto-retry (attempt %d/%d)",
                    deployment_id, retry_count + 1, _MAX_AUTO_RETRIES,
                )
            else:
                # ── Exceeded retries or ZIP gone — fail gracefully ─────
                steps = {}
                raw = data.get("steps_status")
                if raw:
                    try:
                        steps = json.loads(raw)
                    except json.JSONDecodeError:
                        pass

                for step_name, step_status in steps.items():
                    if step_status == "running":
                        steps[step_name] = "failed"
                    elif step_status == "pending":
                        steps[step_name] = "skipped"

                doc.reference.update({
                    "status": "failed",
                    "steps_status": json.dumps(steps),
                    "error_message": (
                        "Le déploiement a été interrompu suite à une maintenance serveur. "
                        "Veuillez relancer le déploiement."
                    ),
                    "completed_at": datetime.now(timezone.utc),
                })

                logger.warning(
                    "Deployment %s cannot be retried (retries=%d, zip_exists=%s) — marked failed",
                    deployment_id, retry_count, zip_path.exists(),
                )

    return to_retry


async def _stale_deployment_watchdog(interval_seconds: int = 120) -> None:
    """
    Background task that periodically checks for deployments stuck in
    'running' state for longer than PIPELINE_MAX_TIMEOUT_SECONDS.

    Runs every *interval_seconds* (default: 2 minutes).
    """
    settings = get_settings()
    max_age = settings.PIPELINE_MAX_TIMEOUT_SECONDS

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            db = SessionLocal()
            query = db.collection("deployments").where("status", "==", "running")
            now = datetime.now(timezone.utc)

            for doc in query.stream():
                data = doc.to_dict()
                started_at = data.get("started_at")
                if started_at is None:
                    continue

                # Firestore returns datetime objects directly
                if hasattr(started_at, 'timestamp'):
                    elapsed = (now - started_at.replace(tzinfo=timezone.utc)).total_seconds()
                else:
                    continue

                if elapsed > max_age:
                    deployment_id = doc.id
                    current_step = data.get("current_step", "UNKNOWN")

                    steps = {}
                    raw = data.get("steps_status")
                    if raw:
                        try:
                            steps = json.loads(raw)
                        except json.JSONDecodeError:
                            pass

                    for step_name, step_status in steps.items():
                        if step_status == "running":
                            steps[step_name] = "failed"
                        elif step_status == "pending":
                            steps[step_name] = "skipped"

                    doc.reference.update({
                        "status": "failed",
                        "steps_status": json.dumps(steps),
                        "error_message": (
                            "Le déploiement a pris trop de temps et a été arrêté automatiquement. "
                            "Veuillez relancer le déploiement."
                        ),
                        "completed_at": now,
                    })

                    logger.warning(
                        "Watchdog: marked deployment %s as failed "
                        "(running for %ds, limit %ds)",
                        deployment_id, int(elapsed), max_age,
                    )

            # Website deletions whose worker died (instance recycled, request
            # timeout) become retryable failures instead of staying "deleting".
            from services.website_service import expire_stale_deletions
            expired = expire_stale_deletions(db, settings)
            if expired:
                logger.warning("Watchdog: %d interrupted website deletion(s) marked retryable", expired)

        except Exception as exc:
            logger.error("Stale deployment watchdog error: %s", exc)


# ── Lifespan (startup / shutdown) ────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: run setup on startup, teardown on shutdown."""
    # Initialise Firebase Admin SDK
    from services.firebase_auth import init_firebase
    try:
        init_firebase(settings=get_settings())
        logger.info("Firebase Admin SDK ready.")
    except Exception:
        logger.warning("Firebase Admin SDK init failed — auth will be unavailable.", exc_info=True)

    logger.info("Initialising database...")
    init_db()
    logger.info("Database ready.")

    # Recover any deployments left in 'running' state from a previous crash
    to_retry = _recover_stale_deployments()
    if to_retry:
        logger.info("Auto-retrying %d interrupted deployment(s).", len(to_retry))
        settings = get_settings()
        for info in to_retry:
            email_list = [
                e.strip() for e in info["notification_emails"].split(",") if e.strip()
            ]
            deploy_config = DeploymentConfig(
                mode=DeploymentMode(info["mode"]),
                website_name=info["website_name"],
                domain=info["domain"],
                notification_emails=email_list,
                deployer_first_name=info.get("deployer_first_name", ""),
                deployer_last_name=info.get("deployer_last_name", ""),
                deployer_email=info.get("deployer_email", ""),
                ai_enabled=info.get("ai_enabled", False),
            )
            orchestrator = PipelineOrchestrator(settings)
            asyncio.create_task(
                orchestrator.run(info["deployment_id"], info["zip_path"], deploy_config),
                name=f"retry-{info['deployment_id'][:8]}",
            )
            logger.info("Retry task created for deployment %s", info["deployment_id"])

    # Start background watchdog
    watchdog_task = asyncio.create_task(
        _stale_deployment_watchdog(),
        name="stale-deployment-watchdog",
    )
    logger.info("Stale deployment watchdog started.")

    # Start report schedulers
    from services.daily_report_service import (
        daily_report_scheduler,
        weekly_report_scheduler,
        monthly_report_scheduler,
    )

    daily_task = asyncio.create_task(
        daily_report_scheduler(),
        name="daily-report-scheduler",
    )
    weekly_task = asyncio.create_task(
        weekly_report_scheduler(),
        name="weekly-report-scheduler",
    )
    monthly_task = asyncio.create_task(
        monthly_report_scheduler(),
        name="monthly-report-scheduler",
    )
    logger.info("Report schedulers started (daily, weekly, monthly).")

    # Start scheduled deployment checker
    from services.scheduled_deployer import scheduled_deployment_task
    scheduled_task = asyncio.create_task(
        scheduled_deployment_task(),
        name="scheduled-deployment-checker",
    )
    logger.info("Scheduled deployment checker started.")

    # Start system monitor
    from services.system_monitor import system_monitor_task
    monitor_task = asyncio.create_task(
        system_monitor_task(),
        name="system-monitor",
    )
    logger.info("System monitor started.")

    yield

    daily_task.cancel()
    weekly_task.cancel()
    monthly_task.cancel()
    watchdog_task.cancel()
    scheduled_task.cancel()
    monitor_task.cancel()
    logger.info("Shutting down WebDeploy.")


# ── Application factory ──────────────────────────────────────────────

app = FastAPI(
    title="WebDeploy",
    description=(
        "Automated website deployment platform. "
        "Upload a ZIP (Vite or static HTML/CSS/JS), and WebDeploy handles "
        "extraction, AI validation, building, GCP infrastructure provisioning, "
        "CDN upload, and email notification."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

# ── CORS — allow the frontend dev server (port 3000) ─────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Development: accept all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Readable by the browser when the frontend is served from another origin
    # (Cloud Run), not just through the Vite dev proxy.
    expose_headers=["X-Total-Count"],
)

# ── Register routers ─────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(deployments_router)
app.include_router(domains_router)
app.include_router(health_router)
app.include_router(statistics_router)
app.include_router(websocket_router)
app.include_router(preview_router)
app.include_router(git_router)
app.include_router(dashboard_router)
app.include_router(quotas_router)
app.include_router(alerts_router)
app.include_router(websites_router)
