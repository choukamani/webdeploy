"""Scheduled deployment service — check and execute scheduled deployments."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config import get_settings
from db.database import SessionLocal
from models.enums import DeploymentMode
from models.deployment import DeploymentConfig

logger = logging.getLogger("webdeploy.scheduled_deployer")

# Check every 30 seconds for due scheduled deployments
_CHECK_INTERVAL = 30


async def scheduled_deployment_task() -> None:
    """Background task that checks for and triggers scheduled deployments."""
    # Wait a bit after startup
    await asyncio.sleep(10)

    while True:
        try:
            await _check_and_trigger()
        except Exception as exc:
            logger.error("Scheduled deployment check failed: %s", exc)

        await asyncio.sleep(_CHECK_INTERVAL)


async def _check_and_trigger() -> None:
    """Find all scheduled deployments that are due and trigger them."""
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    # Single-field query + Python filter to avoid composite index requirement
    query = db.collection("deployments").where("status", "==", "scheduled")
    all_scheduled = list(query.stream())
    docs = []
    for d in all_scheduled:
        data = d.to_dict()
        scheduled_at = data.get("scheduled_at")
        if scheduled_at and hasattr(scheduled_at, "timestamp"):
            sat = scheduled_at.replace(tzinfo=timezone.utc) if scheduled_at.tzinfo is None else scheduled_at
            if sat <= now:
                docs.append(d)
    if not docs:
        return

    settings = get_settings()
    from services.pipeline_orchestrator import PipelineOrchestrator
    from services.zip_backup import restore_zip

    for doc in docs:
        data = doc.to_dict()
        deployment_id = doc.id

        logger.info("Triggering scheduled deployment %s (was due at %s)", deployment_id, data.get("scheduled_at"))

        # Update status to queued
        doc.reference.update({
            "status": "queued",
            "current_step": None,
        })

        # Find the ZIP file
        zip_path = settings.upload_path / f"{deployment_id}.zip"
        if not zip_path.exists():
            try:
                restore_zip(settings, deployment_id, zip_path)
            except Exception:
                logger.warning("Failed to restore ZIP for scheduled deployment %s", deployment_id)
                doc.reference.update({
                    "status": "failed",
                    "error_message": "Le fichier ZIP n'est plus disponible pour le deploiement programme.",
                    "completed_at": now,
                })
                continue

        # Build config
        email_str = data.get("notification_emails", "")
        email_list = [e.strip() for e in email_str.split(",") if e.strip()]

        deploy_config = DeploymentConfig(
            mode=DeploymentMode(data.get("mode", "demo")),
            website_name=data.get("website_name", ""),
            domain=data.get("domain"),
            notification_emails=email_list,
            deployer_first_name=data.get("deployer_first_name", ""),
            deployer_last_name=data.get("deployer_last_name", ""),
            deployer_email=data.get("deployer_email", ""),
            ai_enabled=data.get("ai_enabled", False),
        )

        orchestrator = PipelineOrchestrator(settings)
        asyncio.create_task(
            orchestrator.run(deployment_id, str(zip_path), deploy_config),
            name=f"scheduled-{deployment_id[:8]}",
        )
        logger.info("Scheduled deployment %s triggered", deployment_id)
