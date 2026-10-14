"""Quota management API — define and enforce per-user deployment limits."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from config import Settings, get_settings
from db.database import get_db
from db import crud
from api.dependencies import require_admin, require_approved

logger = logging.getLogger("webdeploy.api.quotas")

router = APIRouter(prefix="/api/quotas", tags=["quotas"])


# ── Models ────────────────────────────────────────────────────────────

class QuotaConfig(BaseModel):
    """Quota configuration for a user or role."""
    max_deployments_per_day: int = 10
    max_zip_size_mb: int = 500
    max_concurrent_deployments: int = 3
    max_total_deployments: int = -1  # -1 = unlimited


class QuotaUpdate(BaseModel):
    """Partial update for quotas."""
    max_deployments_per_day: Optional[int] = None
    max_zip_size_mb: Optional[int] = None
    max_concurrent_deployments: Optional[int] = None
    max_total_deployments: Optional[int] = None


class QuotaResponse(BaseModel):
    target_type: str  # "user" or "role"
    target_id: str    # uid or role name
    target_label: str  # display name or role label
    config: QuotaConfig


class QuotaUsage(BaseModel):
    """Current usage stats for a user."""
    deployments_today: int = 0
    active_deployments: int = 0
    total_deployments: int = 0
    quota: QuotaConfig
    within_limits: bool = True


# ── Default quotas per role ───────────────────────────────────────────

_DEFAULT_QUOTAS = {
    "simple_user": QuotaConfig(
        max_deployments_per_day=5,
        max_zip_size_mb=100,
        max_concurrent_deployments=1,
        max_total_deployments=50,
    ),
    "super_user": QuotaConfig(
        max_deployments_per_day=20,
        max_zip_size_mb=500,
        max_concurrent_deployments=3,
        max_total_deployments=-1,
    ),
    "admin": QuotaConfig(
        max_deployments_per_day=-1,
        max_zip_size_mb=500,
        max_concurrent_deployments=5,
        max_total_deployments=-1,
    ),
}


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/quotas/defaults — get default quotas per role
# ═══════════════════════════════════════════════════════════════════════

def _effective_role_quota(db, role: str) -> QuotaConfig:
    """Return the stored quota for a role, falling back to defaults."""
    stored = crud.get_quota(db, target_type="role", target_id=role)
    if stored:
        return QuotaConfig(**stored)
    return _DEFAULT_QUOTAS.get(role, QuotaConfig())


def _effective_user_quota(db, uid: str, role: str) -> QuotaConfig:
    """Return the effective quota for a user — override wins over role."""
    stored_user = crud.get_quota(db, target_type="user", target_id=uid)
    if stored_user:
        return QuotaConfig(**stored_user)
    return _effective_role_quota(db, role)


@router.get("/defaults")
def get_defaults(user=Depends(require_admin), db=Depends(get_db)):
    """Return the effective quota config per role (admin only).

    This reads the stored value from Firestore when present, falling back to
    the built-in defaults. That way the admin UI always reflects the current
    value and saved changes persist across reloads.
    """
    return {
        role: _effective_role_quota(db, role).model_dump()
        for role in _DEFAULT_QUOTAS.keys()
    }


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/quotas/role/{role} — get quotas for a role
# ═══════════════════════════════════════════════════════════════════════

@router.get("/role/{role}", response_model=QuotaResponse)
def get_role_quota(role: str, user=Depends(require_admin), db=Depends(get_db)):
    """Get the quota config for a role."""
    config = _effective_role_quota(db, role)
    role_labels = {"simple_user": "Utilisateur Simple", "super_user": "Super Utilisateur", "admin": "Administrateur"}
    return QuotaResponse(
        target_type="role",
        target_id=role,
        target_label=role_labels.get(role, role),
        config=config,
    )


# ═══════════════════════════════════════════════════════════════════════
#  PUT /api/quotas/role/{role} — set quotas for a role
# ═══════════════════════════════════════════════════════════════════════

@router.put("/role/{role}", response_model=QuotaResponse)
def set_role_quota(
    role: str,
    body: QuotaUpdate,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """Set quota config for a role (admin only)."""
    valid_roles = {"simple_user", "super_user", "admin"}
    if role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")

    # Get current config as base
    base = _effective_role_quota(db, role)

    # Merge updates
    updates = body.model_dump(exclude_none=True)
    merged = base.model_dump()
    merged.update(updates)

    crud.set_quota(db, target_type="role", target_id=role, config=merged)

    role_labels = {"simple_user": "Utilisateur Simple", "super_user": "Super Utilisateur", "admin": "Administrateur"}
    return QuotaResponse(
        target_type="role",
        target_id=role,
        target_label=role_labels.get(role, role),
        config=QuotaConfig(**merged),
    )


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/quotas/user/{uid} — get quotas for a specific user
# ═══════════════════════════════════════════════════════════════════════

@router.get("/user/{uid}", response_model=QuotaResponse)
def get_user_quota(uid: str, user=Depends(require_admin), db=Depends(get_db)):
    """Get the quota config for a specific user (overrides or falls back to role)."""
    target_user = crud.get_user(db, uid)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found.")

    role = getattr(target_user, "role", "simple_user") or "simple_user"
    config = _effective_user_quota(db, uid, role)
    return QuotaResponse(
        target_type="user",
        target_id=uid,
        target_label=getattr(target_user, "display_name", "") or getattr(target_user, "email", uid),
        config=config,
    )


# ═══════════════════════════════════════════════════════════════════════
#  PUT /api/quotas/user/{uid} — set quotas for a specific user
# ═══════════════════════════════════════════════════════════════════════

@router.put("/user/{uid}", response_model=QuotaResponse)
def set_user_quota(
    uid: str,
    body: QuotaUpdate,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """Set a user-specific quota override (admin only)."""
    target_user = crud.get_user(db, uid)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Get base config (user override or role default)
    role = getattr(target_user, "role", "simple_user") or "simple_user"
    base = _effective_user_quota(db, uid, role)

    updates = body.model_dump(exclude_none=True)
    merged = base.model_dump()
    merged.update(updates)

    crud.set_quota(db, target_type="user", target_id=uid, config=merged)

    return QuotaResponse(
        target_type="user",
        target_id=uid,
        target_label=getattr(target_user, "display_name", "") or getattr(target_user, "email", uid),
        config=QuotaConfig(**merged),
    )


# ═══════════════════════════════════════════════════════════════════════
#  DELETE /api/quotas/user/{uid} — remove user override (revert to role)
# ═══════════════════════════════════════════════════════════════════════

@router.delete("/user/{uid}")
def delete_user_quota(uid: str, user=Depends(require_admin), db=Depends(get_db)):
    """Remove user-specific quota override, reverting to role defaults."""
    crud.delete_quota(db, target_type="user", target_id=uid)
    return {"deleted": True, "uid": uid}


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/quotas/my-usage — current user's quota usage
# ═══════════════════════════════════════════════════════════════════════

@router.get("/my-usage", response_model=QuotaUsage)
def get_my_usage(user=Depends(require_approved), db=Depends(get_db)):
    """Get the current user's quota usage and limits."""
    usage = crud.get_user_quota_usage(db, user.uid, getattr(user, "email", ""))

    role = getattr(user, "role", "simple_user") or "simple_user"
    quota = _effective_user_quota(db, user.uid, role)

    within_limits = True
    if quota.max_deployments_per_day >= 0 and usage["deployments_today"] >= quota.max_deployments_per_day:
        within_limits = False
    if quota.max_concurrent_deployments >= 0 and usage["active_deployments"] >= quota.max_concurrent_deployments:
        within_limits = False
    if quota.max_total_deployments >= 0 and usage["total_deployments"] >= quota.max_total_deployments:
        within_limits = False

    return QuotaUsage(
        deployments_today=usage["deployments_today"],
        active_deployments=usage["active_deployments"],
        total_deployments=usage["total_deployments"],
        quota=quota,
        within_limits=within_limits,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Enforcement helper (used by deployments route)
# ═══════════════════════════════════════════════════════════════════════

def check_quota(db, user) -> None:
    """Raise 429 if the user has exceeded their quota. Call before creating deployment."""
    usage = crud.get_user_quota_usage(db, user.uid, getattr(user, "email", ""))
    role = getattr(user, "role", "simple_user") or "simple_user"
    quota = _effective_user_quota(db, user.uid, role)

    if quota.max_deployments_per_day >= 0 and usage["deployments_today"] >= quota.max_deployments_per_day:
        raise HTTPException(
            status_code=429,
            detail=f"Quota exceeded: maximum {quota.max_deployments_per_day} deployments per day.",
        )
    if quota.max_concurrent_deployments >= 0 and usage["active_deployments"] >= quota.max_concurrent_deployments:
        raise HTTPException(
            status_code=429,
            detail=f"Quota exceeded: maximum {quota.max_concurrent_deployments} concurrent deployments.",
        )
    if quota.max_total_deployments >= 0 and usage["total_deployments"] >= quota.max_total_deployments:
        raise HTTPException(
            status_code=429,
            detail=f"Quota exceeded: maximum {quota.max_total_deployments} total deployments.",
        )


# ═══════════════════════════════════════════════════════════════════════
#  Quota increase requests (user asks admin for more)
# ═══════════════════════════════════════════════════════════════════════

class QuotaIncreaseRequestBody(BaseModel):
    """Payload for POST /api/quotas/request-increase."""
    requested_quota: QuotaUpdate
    reason: str = ""


class QuotaRequestResponse(BaseModel):
    id: str
    uid: str
    email: str
    display_name: str
    role: str
    current_quota: QuotaConfig
    requested_quota: QuotaConfig
    reason: str = ""
    status: str  # pending | approved | rejected
    created_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    admin_note: Optional[str] = None


class QuotaRequestReview(BaseModel):
    """Admin payload when approving/rejecting a request.

    For approvals, ``applied_quota`` overrides what actually gets stored —
    letting the admin grant a different amount than the user asked for.
    """
    applied_quota: Optional[QuotaUpdate] = None
    admin_note: str = ""


def _request_to_response(req) -> QuotaRequestResponse:
    return QuotaRequestResponse(
        id=req.id,
        uid=getattr(req, "uid", ""),
        email=getattr(req, "email", ""),
        display_name=getattr(req, "display_name", ""),
        role=getattr(req, "role", "simple_user"),
        current_quota=QuotaConfig(**(getattr(req, "current_quota", {}) or {})),
        requested_quota=QuotaConfig(**(getattr(req, "requested_quota", {}) or {})),
        reason=getattr(req, "reason", "") or "",
        status=getattr(req, "status", "pending"),
        created_at=getattr(req, "created_at", None),
        reviewed_at=getattr(req, "reviewed_at", None),
        reviewed_by=getattr(req, "reviewed_by", None),
        admin_note=getattr(req, "admin_note", None),
    )


@router.post("/request-increase", response_model=QuotaRequestResponse, status_code=201)
async def request_quota_increase(
    body: QuotaIncreaseRequestBody,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """A user asks for a higher quota. Notifies admins via email + alert."""
    role = getattr(user, "role", "simple_user") or "simple_user"
    current = _effective_user_quota(db, user.uid, role).model_dump()

    # Merge requested values on top of current so the payload always reflects
    # a full target config (unset fields keep the current value).
    updates = body.requested_quota.model_dump(exclude_none=True)
    merged_target = {**current, **updates}

    # Reject no-op requests
    if all(merged_target.get(k) == current.get(k) for k in current.keys()):
        raise HTTPException(
            status_code=400,
            detail="Requested quota must differ from your current quota.",
        )

    display_name = getattr(user, "display_name", "") or getattr(user, "email", "")
    email = getattr(user, "email", "")

    req = crud.create_quota_request(
        db,
        uid=user.uid,
        email=email,
        display_name=display_name,
        role=role,
        current_quota=current,
        requested_quota=merged_target,
        reason=body.reason.strip(),
    )

    # Create an in-platform alert so admins see the request on the dashboard
    try:
        crud.create_alert(
            db,
            severity="info",
            source="quota_request",
            title=f"Demande de quota: {display_name}",
            message=(
                f"{display_name} ({email}) demande une augmentation de quota."
                + (f" Motif: {body.reason.strip()}" if body.reason.strip() else "")
            ),
        )
    except Exception:
        logger.exception("Failed to create alert for quota request %s", req.id)

    # Email admins (collection of approved admin users, plus the configured
    # ADMIN_APPROVAL_EMAIL fallback)
    try:
        admin_emails = crud.list_admin_emails(db)
        if settings.ADMIN_APPROVAL_EMAIL and settings.ADMIN_APPROVAL_EMAIL not in admin_emails:
            admin_emails.append(settings.ADMIN_APPROVAL_EMAIL)

        from services.email_service import EmailService
        email_svc = EmailService(settings=settings)
        await email_svc.send_quota_increase_request(
            admin_emails=admin_emails,
            display_name=display_name,
            user_email=email,
            role=role,
            current_quota=current,
            requested_quota=merged_target,
            reason=body.reason.strip(),
            admin_url=f"{settings.FRONTEND_URL}/admin/quotas",
        )
    except Exception:
        logger.exception("Failed to email admins about quota request %s", req.id)

    return _request_to_response(req)


@router.get("/requests", response_model=list[QuotaRequestResponse])
def list_quota_requests(
    status: Optional[str] = None,
    limit: int = 100,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """List quota-increase requests (admin only)."""
    items = crud.list_quota_requests(db, status=status, limit=limit)
    return [_request_to_response(i) for i in items]


@router.get("/requests/pending-count")
def pending_quota_requests_count(user=Depends(require_admin), db=Depends(get_db)):
    """Badge count for pending quota requests."""
    return {"count": crud.count_pending_quota_requests(db)}


@router.post("/requests/{request_id}/approve", response_model=QuotaRequestResponse)
def approve_quota_request(
    request_id: str,
    body: Optional[QuotaRequestReview] = None,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """Approve a quota-increase request and apply the limits as a user override."""
    req = crud.get_quota_request(db, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if getattr(req, "status", "pending") != "pending":
        raise HTTPException(status_code=400, detail="Request has already been reviewed.")

    # Resolve the final limits: admin override > originally requested
    final = dict(getattr(req, "requested_quota", {}) or {})
    if body and body.applied_quota:
        for k, v in body.applied_quota.model_dump(exclude_none=True).items():
            final[k] = v

    crud.set_quota(db, target_type="user", target_id=req.uid, config=final)
    crud.update_quota_request_status(
        db,
        request_id,
        status="approved",
        reviewed_by=getattr(user, "email", "admin"),
        admin_note=(body.admin_note if body else "") or "",
    )

    updated = crud.get_quota_request(db, request_id)
    return _request_to_response(updated)


@router.post("/requests/{request_id}/reject", response_model=QuotaRequestResponse)
def reject_quota_request(
    request_id: str,
    body: Optional[QuotaRequestReview] = None,
    user=Depends(require_admin),
    db=Depends(get_db),
):
    """Reject a quota-increase request."""
    req = crud.get_quota_request(db, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    if getattr(req, "status", "pending") != "pending":
        raise HTTPException(status_code=400, detail="Request has already been reviewed.")

    crud.update_quota_request_status(
        db,
        request_id,
        status="rejected",
        reviewed_by=getattr(user, "email", "admin"),
        admin_note=(body.admin_note if body else "") or "",
    )
    updated = crud.get_quota_request(db, request_id)
    return _request_to_response(updated)
