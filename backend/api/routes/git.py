"""Git integration API — connect repos, receive webhooks, list push events."""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from pydantic import BaseModel

from config import Settings, get_settings
from db.database import get_db
from db import crud
from models.enums import UserRole
from api.dependencies import require_approved

logger = logging.getLogger("webdeploy.api.git")

router = APIRouter(prefix="/api/git", tags=["git"])


# ── Models ────────────────────────────────────────────────────────────

class GitConnectionCreate(BaseModel):
    provider: str  # "github" or "gitlab"
    repo_url: str
    repo_name: str
    branch: str = "main"
    access_token: str


class GitConnectionResponse(BaseModel):
    id: str
    provider: str
    repo_url: str
    repo_name: str
    branch: str
    webhook_secret: str
    webhook_url: str
    created_at: Optional[datetime] = None
    user_email: str = ""


class PushEventResponse(BaseModel):
    id: str
    connection_id: str
    branch: str
    commit_sha: str
    commit_message: str
    author: str
    timestamp: Optional[datetime] = None
    deployed: bool = False
    deployment_id: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/git/connections — connect a new repo
# ═══════════════════════════════════════════════════════════════════════

@router.post("/connections", response_model=GitConnectionResponse, status_code=201)
def create_connection(
    body: GitConnectionCreate,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Create a new Git connection for the authenticated user."""
    # Only super_user and admin can use git integration
    if user.role == UserRole.SIMPLE_USER.value:
        raise HTTPException(status_code=403, detail="Super user or admin role required.")

    conn = crud.create_git_connection(
        db,
        uid=user.uid,
        user_email=getattr(user, "email", ""),
        provider=body.provider,
        repo_url=body.repo_url,
        repo_name=body.repo_name,
        branch=body.branch,
        access_token=body.access_token,
    )

    webhook_url = f"{settings.FRONTEND_URL.rstrip('/')}/api/git/webhook/{conn.id}"

    return GitConnectionResponse(
        id=conn.id,
        provider=conn.provider,
        repo_url=conn.repo_url,
        repo_name=conn.repo_name,
        branch=conn.branch,
        webhook_secret=conn.webhook_secret,
        webhook_url=webhook_url,
        created_at=conn.created_at,
        user_email=getattr(conn, "user_email", ""),
    )


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/git/connections — list user's connections
# ═══════════════════════════════════════════════════════════════════════

@router.get("/connections", response_model=list[GitConnectionResponse])
def list_connections(
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """List all Git connections for the current user."""
    connections = crud.list_git_connections(db, uid=user.uid)
    results = []
    for conn in connections:
        webhook_url = f"{settings.FRONTEND_URL.rstrip('/')}/api/git/webhook/{conn.id}"
        results.append(GitConnectionResponse(
            id=conn.id,
            provider=conn.provider,
            repo_url=conn.repo_url,
            repo_name=conn.repo_name,
            branch=conn.branch,
            webhook_secret=conn.webhook_secret,
            webhook_url=webhook_url,
            created_at=conn.created_at,
            user_email=getattr(conn, "user_email", ""),
        ))
    return results


# ═══════════════════════════════════════════════════════════════════════
#  DELETE /api/git/connections/{id} — remove a connection
# ═══════════════════════════════════════════════════════════════════════

@router.delete("/connections/{connection_id}")
def delete_connection(
    connection_id: str,
    user=Depends(require_approved),
    db=Depends(get_db),
):
    """Delete a Git connection and its push events."""
    conn = crud.get_git_connection(db, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found.")
    if conn.uid != user.uid and user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Not your connection.")

    crud.delete_git_connection(db, connection_id)
    return {"deleted": True}


# ═══════════════════════════════════════════════════════════════════════
#  PATCH /api/git/connections/{id}/branch — change tracked branch
# ═══════════════════════════════════════════════════════════════════════

class BranchUpdate(BaseModel):
    branch: str

@router.patch("/connections/{connection_id}/branch", response_model=GitConnectionResponse)
def update_branch(
    connection_id: str,
    body: BranchUpdate,
    user=Depends(require_approved),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Update the tracked branch for a Git connection."""
    conn = crud.get_git_connection(db, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found.")
    if conn.uid != user.uid and user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Not your connection.")

    crud.update_git_connection_branch(db, connection_id, body.branch)
    conn.branch = body.branch
    webhook_url = f"{settings.FRONTEND_URL.rstrip('/')}/api/git/webhook/{conn.id}"

    return GitConnectionResponse(
        id=conn.id,
        provider=conn.provider,
        repo_url=conn.repo_url,
        repo_name=conn.repo_name,
        branch=conn.branch,
        webhook_secret=conn.webhook_secret,
        webhook_url=webhook_url,
        created_at=conn.created_at,
        user_email=getattr(conn, "user_email", ""),
    )


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/git/webhook/{connection_id} — GitHub/GitLab push webhook
# ═══════════════════════════════════════════════════════════════════════

@router.post("/webhook/{connection_id}", status_code=200)
async def receive_webhook(
    connection_id: str,
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None),
    x_gitlab_token: Optional[str] = Header(None),
    db=Depends(get_db),
):
    """Receive a push webhook from GitHub or GitLab."""
    conn = crud.get_git_connection(db, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found.")

    body = await request.body()

    # Verify signature
    if conn.provider == "github" and x_hub_signature_256:
        expected = "sha256=" + hmac.new(
            conn.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=403, detail="Invalid signature.")
    elif conn.provider == "gitlab" and x_gitlab_token:
        if x_gitlab_token != conn.webhook_secret:
            raise HTTPException(status_code=403, detail="Invalid token.")

    import json
    payload = json.loads(body)

    # Parse push event
    if conn.provider == "github":
        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        commits = payload.get("commits", [])
        if not commits:
            return {"status": "no commits"}
        latest = commits[-1]
        commit_sha = latest.get("id", "")[:12]
        commit_message = latest.get("message", "")[:200]
        author = latest.get("author", {}).get("name", "")
    elif conn.provider == "gitlab":
        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        commits = payload.get("commits", [])
        if not commits:
            return {"status": "no commits"}
        latest = commits[-1]
        commit_sha = latest.get("id", "")[:12]
        commit_message = latest.get("message", "")[:200]
        author = latest.get("author", {}).get("name", "")
    else:
        return {"status": "unsupported provider"}

    # Only track pushes to the configured branch
    if branch != conn.branch:
        return {"status": "ignored", "reason": f"branch '{branch}' != tracked '{conn.branch}'"}

    # Store push event
    crud.create_push_event(
        db,
        connection_id=connection_id,
        branch=branch,
        commit_sha=commit_sha,
        commit_message=commit_message,
        author=author,
    )

    logger.info("Push event recorded: %s/%s @ %s by %s", conn.repo_name, branch, commit_sha, author)
    return {"status": "recorded", "branch": branch, "commit": commit_sha}


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/git/push-events — list push events for user's connections
# ═══════════════════════════════════════════════════════════════════════

@router.get("/push-events", response_model=list[PushEventResponse])
def list_push_events(
    connection_id: Optional[str] = None,
    user=Depends(require_approved),
    db=Depends(get_db),
):
    """List push events. If connection_id is given, filter by it."""
    if connection_id:
        conn = crud.get_git_connection(db, connection_id)
        if not conn:
            raise HTTPException(status_code=404, detail="Connection not found.")
        if conn.uid != user.uid and user.role != UserRole.ADMIN.value:
            raise HTTPException(status_code=403, detail="Not your connection.")
        events = crud.list_push_events(db, connection_id=connection_id)
    else:
        # Get all connections for this user, then all their events
        connections = crud.list_git_connections(db, uid=user.uid)
        events = []
        for conn in connections:
            events.extend(crud.list_push_events(db, connection_id=conn.id))
        events.sort(key=lambda e: getattr(e, "timestamp", datetime.min), reverse=True)

    return [
        PushEventResponse(
            id=e.id,
            connection_id=e.connection_id,
            branch=e.branch,
            commit_sha=e.commit_sha,
            commit_message=e.commit_message,
            author=e.author,
            timestamp=getattr(e, "timestamp", None),
            deployed=getattr(e, "deployed", False),
            deployment_id=getattr(e, "deployment_id", None),
        )
        for e in events[:50]  # Limit to 50 most recent
    ]


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/git/push-events/{event_id}/deploy — deploy from push event
# ═══════════════════════════════════════════════════════════════════════

@router.post("/push-events/{event_id}/mark-deployed")
def mark_event_deployed(
    event_id: str,
    deployment_id: str,
    user=Depends(require_approved),
    db=Depends(get_db),
):
    """Mark a push event as deployed (called after deployment is created)."""
    crud.mark_push_event_deployed(db, event_id, deployment_id)
    return {"status": "ok"}
