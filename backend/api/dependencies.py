"""FastAPI dependencies — database session, settings, logging, auth."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import Settings, get_settings
from db.database import get_db
from db import crud
from models.enums import DeploymentMode, UserRole, UserStatus
from services.firebase_auth import verify_firebase_token

logger = logging.getLogger("webdeploy")

_bearer_scheme = HTTPBearer(auto_error=False)

# ── WebSocket log broadcast ──────────────────────────────────────────
# Maps deployment_id → set of asyncio.Queue (one per connected WS client)
_ws_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


def subscribe_logs(deployment_id: str) -> asyncio.Queue:
    """Register a new WebSocket client for real-time logs."""
    q: asyncio.Queue = asyncio.Queue()
    _ws_subscribers[deployment_id].add(q)
    count = len(_ws_subscribers[deployment_id])
    logger.info("[WS] Subscriber added for %s (total: %d)", deployment_id[:8], count)
    return q


def unsubscribe_logs(deployment_id: str, q: asyncio.Queue) -> None:
    _ws_subscribers[deployment_id].discard(q)
    if not _ws_subscribers[deployment_id]:
        del _ws_subscribers[deployment_id]
        logger.info("[WS] All subscribers removed for %s", deployment_id[:8])


def _broadcast_sync(deployment_id: str, message: str) -> None:
    """Push a log line to all WS subscribers (must run on the event-loop thread)."""
    subs = list(_ws_subscribers.get(deployment_id, []))
    for q in subs:
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass  # Drop if client is slow


async def broadcast_log(deployment_id: str, message: str) -> None:
    """Push a log line to all WebSocket subscribers for a deployment."""
    _broadcast_sync(deployment_id, message)


def get_log_callback(deployment_id: str) -> Callable:
    """
    Returns a callable that:
      1. Logs to Python logger
      2. Broadcasts to WebSocket subscribers (thread-safe)

    The callback is safe to call from any thread (e.g. inside
    asyncio.to_thread workers) because it uses call_soon_threadsafe
    to schedule the broadcast on the main event loop.
    """
    # Capture the event loop at creation time — we are on the async thread
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        logger.warning("[WS] get_log_callback(%s): NO event loop captured!", deployment_id[:8])
    else:
        logger.info("[WS] get_log_callback(%s): event loop captured OK", deployment_id[:8])

    def _log(message: str, level: str = "INFO", step: str | None = None) -> None:
        logger.log(logging.getLevelName(level), "[%s] %s", deployment_id[:8], message)

        # Broadcast to WebSocket clients — thread-safe
        # NOTE: Do NOT check loop.is_running() — it's not thread-safe
        # and returns False from worker threads, silently skipping broadcasts.
        # Instead, just try call_soon_threadsafe and catch RuntimeError if
        # the loop has been closed.
        if loop is not None:
            try:
                loop.call_soon_threadsafe(_broadcast_sync, deployment_id, message)
            except RuntimeError:
                pass  # Loop closed

    return _log


# ── Auth dependencies ─────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db=Depends(get_db),
):
    """Extract and verify Bearer token, then look up the Firestore user profile.

    Returns a SimpleNamespace with user fields (uid, email, role, status, …).
    Raises 401 if the token is missing/invalid or the user doesn't exist in Firestore.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    try:
        claims = verify_firebase_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    user = crud.get_user(db, claims["uid"])
    if user is None:
        # User authenticated via Firebase but hasn't signed up in our system yet
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not_registered",
        )
    return user


def require_approved(user=Depends(get_current_user)):
    """Ensure the current user has been approved."""
    if user.status != UserStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Account not approved (status={user.status}).",
        )
    return user


def require_admin(user=Depends(require_approved)):
    """Ensure the current user is an approved admin."""
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


def require_deploy_permission(mode: str, user):
    """Check that the user's role allows deploying in the given mode.

    Simple users can only deploy in demo mode.
    Super users and admins can deploy in any mode.
    """
    if user.role == UserRole.SIMPLE_USER.value and mode != DeploymentMode.DEMO.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Simple users can only deploy in demo mode.",
        )
