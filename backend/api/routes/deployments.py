"""
Deployment API routes — create, list, inspect, and stream logs for deployments.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import uuid
import zipfile
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import aiofiles
from fastapi import (
    APIRouter, Body, Depends, File, Form, HTTPException, Query, Response,
    UploadFile, status,
)
from pydantic import BaseModel, Field
from config import Settings, get_settings
from db.database import get_db
from db import crud
from models.deployment import (
    DeploymentConfig,
    DeploymentCreateResponse,
    DeploymentResponse,
    LogEntry,
)
from models.enums import DeploymentMode, DeploymentStatus, UserRole
from services.pipeline_orchestrator import PipelineOrchestrator
from services.zip_backup import backup_zip
from api.dependencies import require_approved, require_deploy_permission

logger = logging.getLogger("webdeploy.api.deployments")

router = APIRouter(prefix="/api", tags=["deployments"])

# Pre-compiled slug validation pattern
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

# Calendar days in date filters are Paris days, matching the statistics
# endpoint and the daily report so the same query means the same thing.
PARIS_TZ = zoneinfo.ZoneInfo("Europe/Paris")

# History page size. The old hard-coded 100 silently hid everything older;
# callers now get a large page plus an X-Total-Count header, so truncation is
# visible instead of invisible.
DEFAULT_HISTORY_LIMIT = 1000
MAX_HISTORY_LIMIT = 5000

_ALL_MODES = {m.value for m in DeploymentMode}
_ALL_STATUSES = {s.value for s in DeploymentStatus}

# Only finished deployments can be swept up in bulk; queued/running ones are
# still being written to by the pipeline.
_DELETABLE_STATUSES = (DeploymentStatus.SUCCESS.value, DeploymentStatus.FAILED.value)

# Ceiling per bulk request — keeps one click from tearing down the estate, and
# keeps the request inside a sane response time.
MAX_BULK_DELETE = 200



# ═══════════════════════════════════════════════════════════════════════
#  POST /api/deploy — create a new deployment
# ═══════════════════════════════════════════════════════════════════════

def _prepare_website(db, settings: Settings, mode: str, website_name: str, domain: Optional[str]):
    """Validate the target website and return ``(website_key, domain)``.

    Refuses up front (409) when the website is being deleted or already has a
    deployment in flight: two pipelines uploading into the same bucket
    interleave their files, and deploying into a half-deleted website would
    resurrect parts of it.
    """
    from infra.layout import normalize_domain, website_key
    from services import website_service as ws

    if len(website_name) > 63:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="website_name must be at most 63 characters.",
        )
    if "--" in website_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="website_name may not contain '--'.",
        )
    domain = normalize_domain(domain) or None
    reserved = {
        settings.DEMO_DOMAIN, f"www.{settings.DEMO_DOMAIN}",
        settings.SITES_DEMO_HOST, settings.SITES_APPS_DOMAIN,
    }
    if mode == DeploymentMode.PROD.value and domain in reserved:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{domain}' hosts the platform's own demo sites and cannot be deployed to.",
        )
    if mode == DeploymentMode.CLOUDRUN.value:
        host = f"{website_name}.{settings.SITES_APPS_DOMAIN}"
        if website_name == "www" or host == settings.SITES_DEMO_HOST:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{website_name}' is reserved — choose another website name.",
            )
    key = website_key(mode, website_name, domain, settings)
    try:
        ws.check_deployable(db, key, settings)
        if mode == DeploymentMode.CLOUDRUN.value:
            ws.check_cloud_run_name_free(db, key, settings)
    except ws.WebsiteConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return key, domain


@router.post("/deploy", response_model=DeploymentCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_deployment(
    zip_file: Optional[UploadFile] = File(None),
    files: list[UploadFile] = File(default=[]),
    mode: str = Form(...),
    website_name: str = Form(...),
    domain: Optional[str] = Form(None),
    notification_emails: Optional[str] = Form(None),
    deployer_first_name: str = Form(""),
    deployer_last_name: str = Form(""),
    deployer_email: str = Form(""),
    ai_enabled: str = Form("false"),
    domain_purchase_confirmed: str = Form("false"),
    scheduled_at: Optional[str] = Form(None),
    user=Depends(require_approved),
    db = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DeploymentCreateResponse:
    """
    Accept a ZIP upload, raw file(s), or a folder and kick off the
    deployment pipeline.

    Upload types:
    - ``zip_file``: a single .zip archive (existing behaviour)
    - ``files``: one or more raw files (single HTML or folder via
      ``webkitdirectory``).  These are packaged into a ZIP on the fly.

    The pipeline runs asynchronously in the background; the response is
    returned immediately with a ``deployment_id`` the client can poll or
    subscribe to via WebSocket.
    """
    # ── Validate mode ─────────────────────────────────────────────────
    if mode not in (DeploymentMode.DEMO.value, DeploymentMode.PROD.value, DeploymentMode.CLOUDRUN.value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid mode '{mode}'. Must be 'demo', 'prod', or 'cloudrun'.",
        )

    # ── Enforce deploy permissions by role ─────────────────────────────
    require_deploy_permission(mode, user)

    # ── Enforce quotas ─────────────────────────────────────────────────
    from api.routes.quotas import check_quota
    check_quota(db, user)

    # ── Auto-fill deployer info from user profile ──────────────────────
    deployer_email = deployer_email.strip() or getattr(user, "email", "")
    if not deployer_first_name.strip() and hasattr(user, "display_name") and user.display_name:
        parts = user.display_name.split(" ", 1)
        deployer_first_name = parts[0]
        deployer_last_name = parts[1] if len(parts) > 1 else deployer_last_name

    # ── Validate website_name is slug-safe ────────────────────────────
    website_name_lower = website_name.lower().strip()
    if len(website_name_lower) < 2 or not _SLUG_RE.match(website_name_lower):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid website_name '{website_name}'. "
                "Must be lowercase, alphanumeric with hyphens, and at least 2 characters "
                "(e.g. 'my-site')."
            ),
        )

    # ── Validate domain for prod mode ─────────────────────────────────
    if mode == DeploymentMode.PROD.value and not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A domain is required for production deployments.",
        )
    if mode != DeploymentMode.PROD.value:
        domain = None  # only prod websites are keyed by domain
    website_key, domain = await asyncio.to_thread(
        _prepare_website, db, settings, mode, website_name_lower, domain,
    )

    # ── Determine upload type and validate ────────────────────────────
    has_zip = zip_file is not None and zip_file.filename
    has_files = len(files) > 0 and any(f.filename for f in files)

    if not has_zip and not has_files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No file uploaded. Provide either a zip_file or one or more files.",
        )

    # ── Generate deployment ID ────────────────────────────────────────
    deployment_id = str(uuid.uuid4())
    zip_dest = settings.upload_path / f"{deployment_id}.zip"
    stored_zip_filename: str

    if has_zip:
        # ── ZIP upload — existing flow ────────────────────────────────
        if not zip_file.filename.lower().endswith(".zip"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Uploaded file must be a .zip archive.",
            )

        try:
            async with aiofiles.open(str(zip_dest), "wb") as f:
                while chunk := await zip_file.read(1024 * 1024):  # 1 MB chunks
                    await f.write(chunk)
        except Exception as exc:
            logger.exception("Failed to save uploaded ZIP for deployment %s", deployment_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to save uploaded file: {exc}",
            ) from exc

        stored_zip_filename = zip_file.filename
        logger.info(
            "Saved ZIP for deployment %s (%s) -> %s",
            deployment_id, zip_file.filename, zip_dest,
        )
    else:
        # ── Raw file(s) upload — package into ZIP on the fly ──────────
        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                # Detect common folder prefix from webkitdirectory uploads
                # (e.g. "myFolder/index.html", "myFolder/css/style.css")
                # so we can strip it and keep a clean structure in the ZIP.
                names = [upload.filename for upload in files if upload.filename]
                common_prefix = ""
                if len(names) > 1:
                    parts_list = [n.replace("\\", "/").split("/") for n in names]
                    if all(len(p) > 1 for p in parts_list):
                        first_segment = parts_list[0][0]
                        if all(p[0] == first_segment for p in parts_list):
                            common_prefix = first_segment + "/"

                for upload in files:
                    content = await upload.read()
                    arcname = upload.filename.replace("\\", "/")
                    if common_prefix and arcname.startswith(common_prefix):
                        arcname = arcname[len(common_prefix):]
                    if not arcname:
                        continue
                    zf.writestr(arcname, content)
            buf.seek(0)

            async with aiofiles.open(str(zip_dest), "wb") as f:
                await f.write(buf.getvalue())
        except Exception as exc:
            logger.exception("Failed to package files for deployment %s", deployment_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to package uploaded files: {exc}",
            ) from exc

        stored_zip_filename = f"{website_name_lower}-uploaded-files.zip"
        logger.info(
            "Packaged %d file(s) into ZIP for deployment %s -> %s",
            len(files), deployment_id, zip_dest,
        )

    # ── Backup ZIP to GCS (resilience across container restarts) ─────
    try:
        await asyncio.to_thread(backup_zip, settings, deployment_id, zip_dest)
    except Exception:
        logger.warning("GCS ZIP backup failed for %s (non-fatal)", deployment_id, exc_info=True)

    # ── Parse notification emails ─────────────────────────────────────
    email_list: list[str] = []
    if notification_emails:
        email_list = [e.strip() for e in notification_emails.split(",") if e.strip()]
    # Auto-include deployer email in notifications
    deployer_email_stripped = deployer_email.strip()
    if deployer_email_stripped and deployer_email_stripped not in email_list:
        email_list.append(deployer_email_stripped)

    # Merge deployer email into the stored notification_emails string
    all_emails_str = ", ".join(email_list) if email_list else ""

    # ── Parse AI toggle ───────────────────────────────────────────────
    ai_enabled_bool = ai_enabled.lower() == "true"
    domain_purchase_confirmed_bool = domain_purchase_confirmed.lower() == "true"

    # ── Create DB record ──────────────────────────────────────────────
    crud.create_deployment(
        db,
        deployment_id=deployment_id,
        website_name=website_name_lower,
        mode=mode,
        domain=domain,
        notification_emails=all_emails_str,
        zip_filename=stored_zip_filename,
        deployer_first_name=deployer_first_name.strip(),
        deployer_last_name=deployer_last_name.strip(),
        deployer_email=deployer_email_stripped,
        ai_enabled=ai_enabled_bool,
        website_key=website_key,
    )

    # ── Build pipeline config ─────────────────────────────────────────
    deploy_config = DeploymentConfig(
        mode=DeploymentMode(mode),
        website_name=website_name_lower,
        domain=domain,
        notification_emails=email_list,
        deployer_first_name=deployer_first_name.strip(),
        deployer_last_name=deployer_last_name.strip(),
        deployer_email=deployer_email_stripped,
        ai_enabled=ai_enabled_bool,
        domain_purchase_confirmed=domain_purchase_confirmed_bool,
    )

    # ── Handle scheduled deployment ──────────────────────────────────
    if scheduled_at:
        from datetime import datetime as dt
        try:
            schedule_time = dt.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            if schedule_time.tzinfo is None:
                from datetime import timezone as tz
                schedule_time = schedule_time.replace(tzinfo=tz.utc)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid scheduled_at format: '{scheduled_at}'. Use ISO 8601.",
            )

        # Update the deployment record with scheduled status
        db.collection("deployments").document(deployment_id).update({
            "status": "scheduled",
            "scheduled_at": schedule_time,
        })
        logger.info("Deployment %s scheduled for %s", deployment_id, schedule_time.isoformat())
        return DeploymentCreateResponse(deployment_id=deployment_id, status="scheduled")

    # ── Launch the pipeline in the background ─────────────────────────
    orchestrator = PipelineOrchestrator(settings)
    asyncio.create_task(
        orchestrator.run(deployment_id, str(zip_dest), deploy_config),
        name=f"pipeline-{deployment_id[:8]}",
    )

    logger.info("Deployment %s queued (mode=%s, website=%s)", deployment_id, mode, website_name_lower)
    return DeploymentCreateResponse(deployment_id=deployment_id, status="queued")


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/deploy/from-git — deploy a commit from a connected Git repo
# ═══════════════════════════════════════════════════════════════════════

class DeployFromGitRequest(BaseModel):
    """Request body for /api/deploy/from-git."""
    connection_id: str = Field(..., description="Git connection ID")
    commit_sha: str = Field(..., description="Commit SHA or branch/tag ref")
    push_event_id: Optional[str] = Field(None, description="Optional push event to mark as deployed")
    mode: str
    website_name: str
    domain: Optional[str] = None
    notification_emails: Optional[str] = ""
    deployer_first_name: str = ""
    deployer_last_name: str = ""
    deployer_email: str = ""
    ai_enabled: bool = False
    domain_purchase_confirmed: bool = False


@router.post(
    "/deploy/from-git",
    response_model=DeploymentCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_deployment_from_git(
    body: DeployFromGitRequest,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DeploymentCreateResponse:
    """
    Download a snapshot of a Git commit via the provider API (GitHub/GitLab),
    package it as a ZIP, and kick off the deployment pipeline.

    Auth / permissions:
    - Requires an approved user.
    - Only ``super_user`` and ``admin`` can use Git deployments.
    - The authenticated user must own the git connection (or be admin).
    """
    from services.git_service import download_repo_as_zip, GitDownloadError
    from db import crud as crud_module
    from models.enums import UserRole

    # ── Role check ────────────────────────────────────────────────────
    if user.role == UserRole.SIMPLE_USER.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Git deployments require super_user or admin role.",
        )

    # ── Validate mode ─────────────────────────────────────────────────
    if body.mode not in (DeploymentMode.DEMO.value, DeploymentMode.PROD.value, DeploymentMode.CLOUDRUN.value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid mode '{body.mode}'.",
        )
    require_deploy_permission(body.mode, user)

    # ── Quotas ────────────────────────────────────────────────────────
    from api.routes.quotas import check_quota
    check_quota(db, user)

    # ── Validate website_name ─────────────────────────────────────────
    website_name_lower = body.website_name.lower().strip()
    if len(website_name_lower) < 2 or not _SLUG_RE.match(website_name_lower):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid website_name '{body.website_name}'.",
        )

    # ── Domain for prod ───────────────────────────────────────────────
    if body.mode == DeploymentMode.PROD.value and not body.domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A domain is required for production deployments.",
        )
    website_key, domain = await asyncio.to_thread(
        _prepare_website, db, settings, body.mode, website_name_lower,
        body.domain if body.mode == DeploymentMode.PROD.value else None,
    )

    # ── Lookup git connection ────────────────────────────────────────
    conn = crud_module.get_git_connection(db, body.connection_id)
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Git connection '{body.connection_id}' not found.",
        )
    if conn.uid != user.uid and user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only deploy from your own Git connections.",
        )

    # ── Download repo snapshot ───────────────────────────────────────
    deployment_id = str(uuid.uuid4())
    zip_dest = settings.upload_path / f"{deployment_id}.zip"

    try:
        await asyncio.to_thread(
            download_repo_as_zip,
            provider=conn.provider,
            repo_url=conn.repo_url,
            ref=body.commit_sha,
            access_token=conn.access_token,
            dest_zip_path=zip_dest,
        )
    except GitDownloadError as exc:
        logger.exception("Git download failed for deployment %s", deployment_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to download repo: {exc}",
        ) from exc

    stored_zip_filename = f"{conn.repo_name}-{body.commit_sha[:8]}.zip"

    # ── Backup ZIP to GCS ────────────────────────────────────────────
    try:
        await asyncio.to_thread(backup_zip, settings, deployment_id, zip_dest)
    except Exception:
        logger.warning("GCS ZIP backup failed for %s (non-fatal)", deployment_id, exc_info=True)

    # ── Notification emails ──────────────────────────────────────────
    email_list: list[str] = []
    if body.notification_emails:
        email_list = [e.strip() for e in body.notification_emails.split(",") if e.strip()]
    deployer_email_stripped = (body.deployer_email or getattr(user, "email", "")).strip()
    if deployer_email_stripped and deployer_email_stripped not in email_list:
        email_list.append(deployer_email_stripped)
    all_emails_str = ", ".join(email_list) if email_list else ""

    # ── Deployer fields fallback to user profile ─────────────────────
    first_name = body.deployer_first_name.strip()
    last_name = body.deployer_last_name.strip()
    if not first_name and hasattr(user, "display_name") and user.display_name:
        parts = user.display_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else last_name

    # ── DB record ────────────────────────────────────────────────────
    crud.create_deployment(
        db,
        deployment_id=deployment_id,
        website_name=website_name_lower,
        mode=body.mode,
        domain=domain,
        notification_emails=all_emails_str,
        zip_filename=stored_zip_filename,
        deployer_first_name=first_name,
        deployer_last_name=last_name,
        deployer_email=deployer_email_stripped,
        ai_enabled=body.ai_enabled,
        website_key=website_key,
    )

    # Tag deployment with git metadata for traceability
    db.collection("deployments").document(deployment_id).update({
        "git_connection_id": conn.id,
        "git_commit_sha": body.commit_sha,
        "git_repo_name": conn.repo_name,
        "git_branch": conn.branch,
    })

    # ── Pipeline config ──────────────────────────────────────────────
    deploy_config = DeploymentConfig(
        mode=DeploymentMode(body.mode),
        website_name=website_name_lower,
        domain=domain,
        notification_emails=email_list,
        deployer_first_name=first_name,
        deployer_last_name=last_name,
        deployer_email=deployer_email_stripped,
        ai_enabled=body.ai_enabled,
        domain_purchase_confirmed=body.domain_purchase_confirmed,
    )

    # ── Mark the push event as deployed (if provided) ────────────────
    if body.push_event_id:
        try:
            crud_module.mark_push_event_deployed(db, body.push_event_id, deployment_id)
        except Exception:
            logger.warning("Failed to mark push event %s as deployed", body.push_event_id, exc_info=True)

    # ── Launch pipeline ──────────────────────────────────────────────
    orchestrator = PipelineOrchestrator(settings)
    asyncio.create_task(
        orchestrator.run(deployment_id, str(zip_dest), deploy_config),
        name=f"pipeline-{deployment_id[:8]}",
    )

    logger.info(
        "Git deployment %s queued (repo=%s, commit=%s, mode=%s)",
        deployment_id, conn.repo_name, body.commit_sha[:12], body.mode,
    )
    return DeploymentCreateResponse(deployment_id=deployment_id, status="queued")


# ═══════════════════════════════════════════════════════════════════════
#  DELETE /api/deployments/{deployment_id} — delete the website it built
# ═══════════════════════════════════════════════════════════════════════

@router.delete("/deployments/{deployment_id}", status_code=status.HTTP_200_OK)
async def delete_deployment(
    deployment_id: str,
    scope: str = Query(
        "website", pattern="^(website|record)$",
        description="'website': the website and everything it owns; 'record': this history entry only",
    ),
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """
    Delete the website this deployment belongs to (default), or only this
    history entry.

    - ``scope=website`` removes every GCP resource of the website and every
      one of its deployment records, logs and ZIP backups. If GCP cleanup
      fails part-way the response is 502 and the website is left in
      ``delete_failed``; calling again resumes from the failed step.
    - ``scope=record`` removes just this record (and its logs/backup) and
      never touches resources — for pruning old or failed runs of a website
      that stays live. Refused for the live version or the only version.
    """
    from api.routes.websites import raise_for_outcome, run_deletes
    from services import website_service as ws

    record = crud.get_deployment(db, deployment_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deployment '{deployment_id}' not found.",
        )

    if scope == "record":
        try:
            return await asyncio.to_thread(ws.delete_history_entry, db, settings, deployment_id, user)
        except LookupError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
        except ws.WebsiteForbidden as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
        except ws.WebsiteConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    key = ws.key_of(record, settings)
    result = await run_deletes(db, settings, [key], user)
    outcome = raise_for_outcome(result["results"][0])
    return {**outcome, "deleted": True, "deployment_id": deployment_id, "website_key": key}


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/deployments — list all deployments
# ═══════════════════════════════════════════════════════════════════════

def _resolve_date_window(
    start_date: Optional[str], end_date: Optional[str],
) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Turn ``YYYY-MM-DD`` bounds into UTC instants.

    Both bounds are inclusive calendar days in Europe/Paris; the returned
    ``end_utc`` is the exclusive start of the following day.  Either side may
    be omitted for an open-ended range, and omitting both means "no filter".
    """
    try:
        start_d = date.fromisoformat(start_date) if start_date else None
        end_d = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dates must be in YYYY-MM-DD format.",
        )

    if start_d and end_d and start_d > end_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must be <= end_date.",
        )

    start_utc = (
        datetime(start_d.year, start_d.month, start_d.day, tzinfo=PARIS_TZ)
        .astimezone(timezone.utc)
        if start_d else None
    )
    end_utc = (
        (datetime(end_d.year, end_d.month, end_d.day, tzinfo=PARIS_TZ) + timedelta(days=1))
        .astimezone(timezone.utc)
        if end_d else None
    )
    return start_utc, end_utc


def _validate_choices(values: Optional[list[str]], allowed: set[str], field: str):
    """Reject unknown enum values instead of silently matching nothing."""
    if values is None:
        return None
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown {field}: {', '.join(unknown)}. "
                f"Valid values: {', '.join(sorted(allowed))}."
            ),
        )
    return list(dict.fromkeys(values))


@router.get("/deployments", response_model=list[DeploymentResponse])
def list_deployments(
    response: Response,
    start_date: Optional[str] = Query(
        None, description="Earliest day to include, YYYY-MM-DD (Europe/Paris)",
    ),
    end_date: Optional[str] = Query(
        None, description="Latest day to include, YYYY-MM-DD (Europe/Paris)",
    ),
    mode: Optional[list[str]] = Query(
        None, description="Restrict to these modes; repeat the param for several",
    ),
    limit: int = Query(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    offset: int = Query(0, ge=0),
    user=Depends(require_approved),
    db = Depends(get_db),
) -> list[DeploymentResponse]:
    """
    Return deployments, newest first. Admins see all; others see only their own.

    Optionally restrict to a ``created_at`` date range and/or a set of modes.
    The total number of matching deployments — ignoring limit and offset — is
    returned in the ``X-Total-Count`` header so the caller can tell a full page
    from a truncated one.
    """
    start_utc, end_utc = _resolve_date_window(start_date, end_date)
    modes = _validate_choices(mode, _ALL_MODES, "mode")

    deployer_email = (
        None if user.role == UserRole.ADMIN.value else getattr(user, "email", "")
    )

    records = crud.list_deployments(
        db,
        limit=limit,
        offset=offset,
        start_utc=start_utc,
        end_utc=end_utc,
        deployer_email=deployer_email,
        modes=modes,
    )
    response.headers["X-Total-Count"] = str(
        crud.count_deployments(
            db,
            start_utc=start_utc,
            end_utc=end_utc,
            deployer_email=deployer_email,
            modes=modes,
        )
    )
    return [DeploymentResponse.from_record(r) for r in records]


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/deployments/bulk-delete — preview / delete a selection
# ═══════════════════════════════════════════════════════════════════════

class BulkDeleteRequest(BaseModel):
    """
    Either an explicit list of deployment ``ids``, or filters describing a set.

    The selection is resolved to *websites*: deleting a deployment deletes
    the website it built, with all its versions. ``dry_run`` (the default)
    returns that resolution so the caller can show exactly what goes; the
    UI then deletes the previewed websites in chunks via
    ``POST /api/websites/bulk-delete``.
    """
    ids: Optional[list[str]] = Field(
        None, description="Explicit selection; when set, the filters below are ignored",
    )
    start_date: Optional[str] = Field(None, description="YYYY-MM-DD (Europe/Paris)")
    end_date: Optional[str] = Field(None, description="YYYY-MM-DD (Europe/Paris)")
    modes: Optional[list[str]] = Field(None, description="e.g. ['demo', 'cloudrun']")
    statuses: Optional[list[str]] = Field(
        None, description="Defaults to finished deployments only (success, failed)",
    )
    include_prod: bool = Field(
        False,
        description="Prod websites are real customer domains — skipped unless explicitly included",
    )
    dry_run: bool = True
    expected_count: Optional[int] = Field(
        None, description="Required when dry_run is false; must equal the matched website count",
    )


class BulkDeleteMatch(BaseModel):
    id: str
    website_name: str
    mode: str
    status: str
    created_at: Optional[str] = None
    website_key: Optional[str] = None
    deleted: Optional[bool] = None
    error: Optional[str] = None


class WebsitePreview(BaseModel):
    key: str
    website_name: str = ""
    mode: str = ""
    domain: Optional[str] = None
    url: Optional[str] = None
    placement: Optional[str] = None
    status: str = "active"
    live: bool = False
    versions: int = 0
    matched_ids: list[str] = []
    deployment_ids: list[str] = []
    deployers: list[str] = []
    resources: list[str] = []
    deletable: bool = True
    blocked_reason: Optional[str] = None
    last_delete_error: Optional[str] = None
    # Set when this one deployment could be removed from history on its
    # own instead (it is neither the live nor the only version).
    record_only_allowed: bool = False


class BulkDeleteResponse(BaseModel):
    dry_run: bool
    matched: int
    deleted_count: int = 0
    failed_count: int = 0
    items: list[BulkDeleteMatch]
    websites: list[WebsitePreview] = []
    results: list[dict] = []
    warnings: list[str] = []


def _resolve_bulk_targets(db, body: BulkDeleteRequest, deployer_email: Optional[str]):
    """
    Turn a bulk request into the concrete records it refers to.

    Both selection styles funnel through the same guards, so an explicit id
    list cannot reach anything the filter form could not.
    """
    statuses = _validate_choices(body.statuses, _ALL_STATUSES, "status")
    if statuses is None:
        statuses = list(_DELETABLE_STATUSES)

    live = sorted(set(statuses) & {DeploymentStatus.QUEUED.value, DeploymentStatus.RUNNING.value})
    if live:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Refusing to bulk delete in-flight deployments ({', '.join(live)}). "
                "Wait for them to finish, or delete them one by one."
            ),
        )

    modes = _validate_choices(body.modes, _ALL_MODES, "mode")
    if modes is None:
        modes = sorted(_ALL_MODES)
    if not body.include_prod:
        modes = [m for m in modes if m != DeploymentMode.PROD.value]
    if not modes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No modes left to delete — prod is excluded unless include_prod is set.",
        )

    if body.ids is not None:
        if not body.ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'ids' was provided but empty.",
            )
        records = []
        for deployment_id in dict.fromkeys(body.ids):
            record = crud.get_deployment(db, deployment_id)
            if record is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Deployment '{deployment_id}' not found.",
                )
            if deployer_email is not None and \
                    (getattr(record, "deployer_email", "") or "").casefold() != deployer_email.casefold():
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only delete your own deployments.",
                )
            if getattr(record, "status", "") not in statuses:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Deployment '{deployment_id}' is {record.status}; "
                        f"only {', '.join(statuses)} can be bulk deleted."
                    ),
                )
            if getattr(record, "mode", "") not in modes:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Deployment '{deployment_id}' is mode '{record.mode}', "
                        "which this request does not cover."
                    ),
                )
            records.append(record)
        return records

    if not (body.start_date or body.end_date or body.modes):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Refusing to match every deployment. Give 'ids', or narrow the set "
                "with a date range and/or modes."
            ),
        )

    start_utc, end_utc = _resolve_date_window(body.start_date, body.end_date)
    return crud.list_deployments(
        db,
        limit=MAX_BULK_DELETE + 1,  # one extra so an over-large set is detectable
        start_utc=start_utc,
        end_utc=end_utc,
        deployer_email=deployer_email,
        modes=modes,
        statuses=statuses,
    )


def _website_previews(db, settings: Settings, records: list, user) -> list[WebsitePreview]:
    from services import website_service as ws

    matched: dict[str, list[str]] = {}
    for r in records:
        matched.setdefault(ws.key_of(r, settings), []).append(r.id)

    previews = []
    for summary in ws.describe_many(db, matched.keys(), settings):
        key = summary["key"]
        if not summary.get("exists"):
            continue
        blocked = None
        if summary["deletion_running"]:
            blocked = "A deletion of this website is already running."
        elif summary["in_flight"]:
            blocked = "A deployment of this website is in progress."
        elif getattr(user, "role", None) != UserRole.ADMIN.value:
            me = (getattr(user, "email", "") or "").casefold()
            if any(d.casefold() != me for d in summary["deployers"]):
                blocked = "Other users deployed to this website — only an admin can delete it."
        ids = matched[key]
        previews.append(WebsitePreview(
            key=key,
            website_name=summary["website_name"],
            mode=summary["mode"],
            domain=summary["domain"],
            url=summary["url"],
            placement=summary["placement"],
            status=summary["status"],
            live=summary["live"],
            versions=summary["versions"],
            matched_ids=ids,
            deployment_ids=summary["deployment_ids"],
            deployers=summary["deployers"],
            resources=summary["resources"],
            deletable=blocked is None,
            blocked_reason=blocked,
            last_delete_error=summary["last_delete_error"],
            record_only_allowed=(
                len(ids) == 1 and summary["versions"] > 1
                and ids[0] != summary["current_deployment_id"]
            ),
        ))
    return previews


@router.post("/deployments/bulk-delete", response_model=BulkDeleteResponse)
async def bulk_delete_deployments(
    body: BulkDeleteRequest,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BulkDeleteResponse:
    """
    Resolve a selection (explicit ids, or date range + modes) to the websites
    it would delete, with their versions and resources.

    ``dry_run: false`` deletes them right away, for selections of up to
    ``MAX_WEBSITES_PER_REQUEST`` websites with ``expected_count`` equal to
    the previewed website count; larger selections go through
    ``POST /api/websites/bulk-delete`` in chunks.
    """
    from api.routes.websites import MAX_WEBSITES_PER_REQUEST, run_deletes

    deployer_email = (
        None if user.role == UserRole.ADMIN.value else getattr(user, "email", "")
    )
    records = await asyncio.to_thread(_resolve_bulk_targets, db, body, deployer_email)
    if len(records) > MAX_BULK_DELETE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{MAX_BULK_DELETE}+ deployments match — over the {MAX_BULK_DELETE} "
                "per-request limit. Narrow the date range and delete in batches."
            ),
        )

    from services import website_service as ws

    def _describe(record) -> BulkDeleteMatch:
        created = getattr(record, "created_at", None)
        return BulkDeleteMatch(
            id=record.id,
            website_name=getattr(record, "website_name", ""),
            mode=getattr(record, "mode", ""),
            status=getattr(record, "status", ""),
            created_at=created.isoformat() if created else None,
            website_key=ws.key_of(record, settings),
        )

    websites = await asyncio.to_thread(_website_previews, db, settings, records, user)
    warnings: list[str] = []
    extra = sum(w.versions - len(w.matched_ids) for w in websites)
    if extra > 0:
        warnings.append(
            f"{extra} more deployment record(s) of these websites, outside the selection, "
            "go too: deleting a website removes all of its versions."
        )
    prod = [w for w in websites if w.mode == DeploymentMode.PROD.value]
    if prod:
        warnings.append(
            f"{len(prod)} production website(s) included — their domains stop being served "
            "and their certificates and DNS records are removed."
        )
    blocked = [w for w in websites if not w.deletable]
    if blocked:
        warnings.append(f"{len(blocked)} website(s) cannot be deleted right now (see each one).")

    if body.dry_run:
        return BulkDeleteResponse(
            dry_run=True, matched=len(records), items=[_describe(r) for r in records],
            websites=websites, warnings=warnings,
        )

    targets = [w.key for w in websites if w.deletable]
    if body.expected_count is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expected_count is required when dry_run is false.",
        )
    if body.expected_count != len(targets):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Selection changed: {len(targets)} websites can be deleted now, "
                f"but {body.expected_count} were expected. Preview again."
            ),
        )
    if len(targets) > MAX_WEBSITES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{len(targets)} websites selected — delete them in chunks of "
                f"{MAX_WEBSITES_PER_REQUEST} via POST /api/websites/bulk-delete."
            ),
        )

    logger.warning("Bulk delete of %d website(s) requested by %s", len(targets), getattr(user, "email", "?"))
    result = await run_deletes(db, settings, targets, user)
    return BulkDeleteResponse(
        dry_run=False,
        matched=len(records),
        deleted_count=result["deleted_count"],
        failed_count=result["failed_count"] + result["blocked_count"],
        items=[_describe(r) for r in records],
        websites=websites,
        results=result["results"],
        warnings=warnings,
    )


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/deployments/{deployment_id} — single deployment detail
# ═══════════════════════════════════════════════════════════════════════

def _get_visible_deployment(db, deployment_id: str, user):
    """The record, if it exists and the user may see it — admins see every
    deployment, others their own (the same rule the history list applies)."""
    record = crud.get_deployment(db, deployment_id)
    visible = record is not None and (
        user.role == UserRole.ADMIN.value
        or (getattr(record, "deployer_email", "") or "").casefold()
        == (getattr(user, "email", "") or "").casefold()
    )
    if not visible:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deployment '{deployment_id}' not found.",
        )
    return record


@router.get("/deployments/{deployment_id}", response_model=DeploymentResponse)
def get_deployment(
    deployment_id: str,
    user=Depends(require_approved),
    db = Depends(get_db),
) -> DeploymentResponse:
    """Return details for a single deployment."""
    return DeploymentResponse.from_record(_get_visible_deployment(db, deployment_id, user))


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/deployments/{deployment_id}/logs — deployment logs
# ═══════════════════════════════════════════════════════════════════════

@router.get("/deployments/{deployment_id}/logs", response_model=list[LogEntry])
def get_deployment_logs(
    deployment_id: str,
    user=Depends(require_approved),
    db = Depends(get_db),
) -> list[LogEntry]:
    """Return all log entries for a deployment, ordered by timestamp."""
    _get_visible_deployment(db, deployment_id, user)

    log_records = crud.get_logs(db, deployment_id)
    return [
        LogEntry(
            timestamp=lr.timestamp,
            level=lr.level,
            step=lr.step,
            message=lr.message,
        )
        for lr in log_records
    ]
