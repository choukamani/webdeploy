"""Authentication API routes — signup, profile, user approval/rejection."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr

from config import Settings, get_settings
from db.database import get_db
from db import crud
from models.enums import UserRole, UserStatus
from models.user import UserCreate, UserResponse, NotificationPreferences, NotificationPreferencesUpdate
from services.firebase_auth import verify_firebase_token
from services.approval_tokens import generate_approval_token, verify_approval_token
from api.dependencies import get_current_user, require_approved, require_admin

logger = logging.getLogger("webdeploy.api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer_scheme = HTTPBearer(auto_error=False)


class GoogleSignInRequest(BaseModel):
    credential: str  # Google ID token from Google Identity Services


class ApproveUserRequest(BaseModel):
    """Optional body for approve endpoint — admin may override requested role."""
    role: Optional[UserRole] = None


class UpdateRoleRequest(BaseModel):
    role: UserRole


class AdminCreateUserRequest(BaseModel):
    email: EmailStr
    display_name: str = ""
    role: UserRole = UserRole.SIMPLE_USER


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/auth/google-signin — exchange Google ID token for Firebase custom token
# ═══════════════════════════════════════════════════════════════════════

@router.post("/google-signin")
async def google_signin(
    body: GoogleSignInRequest,
    settings: Settings = Depends(get_settings),
):
    """Exchange a Google ID token (from GIS) for a Firebase Custom Token.

    This bypasses the need for Firebase's signInWithPopup and its OAuth
    redirect URI requirements.  The frontend uses Google Identity Services
    to obtain a Google credential, sends it here, and receives a Firebase
    custom token it can use with ``signInWithCustomToken()``.
    """
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_requests
    from firebase_admin import auth as fb_auth

    # 1. Verify the Google ID token
    try:
        idinfo = google_id_token.verify_oauth2_token(
            body.credential,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {exc}")

    email = idinfo.get("email")
    name = idinfo.get("name", "")
    picture = idinfo.get("picture", "")

    if not email:
        raise HTTPException(status_code=400, detail="Email not found in Google token.")

    # 2. Get or create Firebase Auth user
    try:
        fb_user = fb_auth.get_user_by_email(email)
        uid = fb_user.uid
        # Update profile if Google info changed
        updates = {}
        if name and fb_user.display_name != name:
            updates["display_name"] = name
        if picture and fb_user.photo_url != picture:
            updates["photo_url"] = picture
        if updates:
            fb_auth.update_user(uid, **updates)
    except fb_auth.UserNotFoundError:
        fb_user = fb_auth.create_user(
            email=email,
            display_name=name,
            photo_url=picture,
            email_verified=True,
        )
        uid = fb_user.uid

    # 3. Create Firebase Custom Token
    custom_token = fb_auth.create_custom_token(uid)
    token_str = custom_token.decode("utf-8") if isinstance(custom_token, bytes) else custom_token

    logger.info("Google sign-in: issued custom token for %s (%s)", email, uid)
    return {"custom_token": token_str}


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/auth/signup — register after Firebase login
# ═══════════════════════════════════════════════════════════════════════

@router.post("/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    body: UserCreate,
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Create a pending user account after Firebase Google sign-in.

    Sends an approval request email to the admin.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        claims = verify_firebase_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    uid = claims["uid"]
    email = claims["email"]
    display_name = claims["name"]

    # Check if user already exists
    existing = crud.get_user(db, uid)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already registered.",
        )

    requested_role = body.requested_role

    user = crud.create_user(
        db,
        uid=uid,
        email=email,
        display_name=display_name,
        requested_role=requested_role.value,
    )

    # Send approval request email to admin
    try:
        approve_token = generate_approval_token(uid, "approve")
        reject_token = generate_approval_token(uid, "reject")
        approve_url = f"{settings.FRONTEND_URL}/auth/action?uid={uid}&action=approve&token={approve_token}"
        reject_url = f"{settings.FRONTEND_URL}/auth/action?uid={uid}&action=reject&token={reject_token}"

        from services.email_service import EmailService
        email_svc = EmailService(settings=settings)
        await email_svc.send_approval_request(
            admin_email=settings.ADMIN_APPROVAL_EMAIL,
            display_name=display_name,
            user_email=email,
            requested_role=requested_role.value,
            approve_url=approve_url,
            reject_url=reject_url,
        )
    except Exception:
        logger.exception("Failed to send approval request email for %s", uid)

    return UserResponse.from_record(user)


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/auth/me — current user profile
# ═══════════════════════════════════════════════════════════════════════

@router.get("/me", response_model=UserResponse)
def get_me(user=Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return UserResponse.from_record(user)


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/auth/approve/{uid} — approve a user
# ═══════════════════════════════════════════════════════════════════════

@router.post("/approve/{uid}", response_model=UserResponse)
async def approve_user(
    uid: str,
    body: Optional[ApproveUserRequest] = Body(None),
    token: Optional[str] = Query(None),
    role: Optional[UserRole] = Query(None),
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Approve a pending user. Auth via admin session OR email token.

    The admin may override the requested role by passing ``role`` in the JSON
    body (session auth) or as a query parameter (email-link flow).
    """
    _authorize_action(uid, "approve", token, credentials, db)

    target = crud.get_user(db, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # Role resolution priority: explicit body > query param > requested > default
    override_role = (body.role if body and body.role else None) or role
    if override_role is not None:
        final_role = override_role.value
    else:
        final_role = target.requested_role or UserRole.SIMPLE_USER.value
    crud.update_user_status(db, uid, status=UserStatus.APPROVED.value, role=final_role, approved_by="admin")
    role_for_email = final_role

    # Notify the user (if they haven't disabled account notifications)
    try:
        prefs = crud.get_notification_preferences(db, uid) or {}
        if prefs.get("account_notifications", True) is not False:
            from services.email_service import EmailService
            email_svc = EmailService(settings=settings)
            await email_svc.send_status_notification(
                to_email=target.email,
                display_name=getattr(target, "display_name", ""),
                user_status=UserStatus.APPROVED.value,
                role=role_for_email,
            )
    except Exception:
        logger.exception("Failed to send approval notification to %s", target.email)

    updated = crud.get_user(db, uid)
    return UserResponse.from_record(updated)


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/auth/reject/{uid} — reject a user
# ═══════════════════════════════════════════════════════════════════════

@router.post("/reject/{uid}", response_model=UserResponse)
async def reject_user(
    uid: str,
    token: Optional[str] = Query(None),
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Reject a pending user. Auth via admin session OR email token."""
    _authorize_action(uid, "reject", token, credentials, db)

    target = crud.get_user(db, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    crud.update_user_status(db, uid, status=UserStatus.REJECTED.value)

    # Notify the user (if they haven't disabled account notifications)
    try:
        prefs = crud.get_notification_preferences(db, uid) or {}
        if prefs.get("account_notifications", True) is not False:
            from services.email_service import EmailService
            email_svc = EmailService(settings=settings)
            await email_svc.send_status_notification(
                to_email=target.email,
                display_name=getattr(target, "display_name", ""),
                user_status=UserStatus.REJECTED.value,
                role=None,
            )
    except Exception:
        logger.exception("Failed to send rejection notification to %s", target.email)

    updated = crud.get_user(db, uid)
    return UserResponse.from_record(updated)


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/auth/me/notification-preferences — get notification prefs
# ═══════════════════════════════════════════════════════════════════════

@router.get("/me/notification-preferences", response_model=NotificationPreferences)
def get_notification_prefs(user=Depends(get_current_user), db=Depends(get_db)):
    """Return the current user's notification preferences."""
    raw = crud.get_notification_preferences(db, user.uid)
    if raw:
        return NotificationPreferences(**raw)
    return NotificationPreferences()


# ═══════════════════════════════════════════════════════════════════════
#  PUT /api/auth/me/notification-preferences — update notification prefs
# ═══════════════════════════════════════════════════════════════════════

@router.put("/me/notification-preferences", response_model=NotificationPreferences)
def update_notification_prefs(
    body: NotificationPreferencesUpdate,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Update the current user's notification preferences."""
    # Validate report_frequency if provided
    valid_frequencies = {"daily", "weekly", "monthly", "custom"}
    if body.report_frequency and body.report_frequency not in valid_frequencies:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid report_frequency. Must be one of: {', '.join(valid_frequencies)}",
        )

    updates = body.model_dump(exclude_none=True)
    merged = crud.update_notification_preferences(db, user.uid, updates)
    return NotificationPreferences(**merged)


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/auth/users — list all users (admin only)
# ═══════════════════════════════════════════════════════════════════════

@router.get("/users", response_model=list[UserResponse])
def list_users(user=Depends(require_admin), db=Depends(get_db)):
    """Return all registered users (admin only)."""
    records = crud.list_users(db)
    return [UserResponse.from_record(r) for r in records]


# ═══════════════════════════════════════════════════════════════════════
#  PATCH /api/auth/users/{uid}/role — change role of an approved user
# ═══════════════════════════════════════════════════════════════════════

@router.patch("/users/{uid}/role", response_model=UserResponse)
def update_user_role(
    uid: str,
    body: UpdateRoleRequest,
    admin=Depends(require_admin),
    db=Depends(get_db),
):
    """Change a user's effective role (admin only)."""
    target = crud.get_user(db, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.status != UserStatus.APPROVED.value:
        raise HTTPException(
            status_code=400,
            detail="Le rôle ne peut être changé que pour un utilisateur approuvé.",
        )
    crud.update_user_role(db, uid, body.role.value)
    updated = crud.get_user(db, uid)
    return UserResponse.from_record(updated)


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/auth/users — admin creates a user
# ═══════════════════════════════════════════════════════════════════════

@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_user(
    body: AdminCreateUserRequest,
    admin=Depends(require_admin),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Create a user directly (admin action).

    The user is created in Firebase Auth (if not already present) and marked
    approved in Firestore with the given role. On next Google sign-in they
    will land with the role already assigned.
    """
    from firebase_admin import auth as fb_auth

    email = body.email.lower().strip()
    display_name = body.display_name.strip()

    # Get or create the Firebase Auth user
    try:
        fb_user = fb_auth.get_user_by_email(email)
        uid = fb_user.uid
        if display_name and fb_user.display_name != display_name:
            try:
                fb_auth.update_user(uid, display_name=display_name)
            except Exception:
                logger.warning("Failed to update Firebase display_name for %s", uid)
    except fb_auth.UserNotFoundError:
        fb_user = fb_auth.create_user(
            email=email,
            display_name=display_name,
            email_verified=False,
        )
        uid = fb_user.uid

    # Reject if already in Firestore
    existing = crud.get_user(db, uid)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Un utilisateur avec l'email {email} existe déjà.",
        )

    user = crud.create_user_approved(
        db,
        uid=uid,
        email=email,
        display_name=display_name or email,
        role=body.role.value,
        approved_by=getattr(admin, "email", "admin"),
    )

    # Best-effort status email to let the user know their account is ready
    try:
        from services.email_service import EmailService
        email_svc = EmailService(settings=settings)
        await email_svc.send_status_notification(
            to_email=email,
            display_name=display_name or email,
            user_status=UserStatus.APPROVED.value,
            role=body.role.value,
        )
    except Exception:
        logger.exception("Failed to send invitation email to %s", email)

    return UserResponse.from_record(user)


# ═══════════════════════════════════════════════════════════════════════
#  DELETE /api/auth/users/{uid} — admin deletes a user
# ═══════════════════════════════════════════════════════════════════════

@router.delete("/users/{uid}", status_code=status.HTTP_200_OK)
def admin_delete_user(
    uid: str,
    admin=Depends(require_admin),
    db=Depends(get_db),
):
    """Delete a user from Firestore and (best-effort) from Firebase Auth."""
    if uid == getattr(admin, "uid", None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vous ne pouvez pas supprimer votre propre compte.",
        )

    target = crud.get_user(db, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # Firestore
    crud.delete_user(db, uid)

    # Firebase Auth (best-effort)
    try:
        from firebase_admin import auth as fb_auth
        fb_auth.delete_user(uid)
    except Exception:
        logger.warning("Failed to delete Firebase Auth user %s (non-fatal)", uid, exc_info=True)

    return {"deleted": True, "uid": uid}


# ── Private helper ────────────────────────────────────────────────────

def _authorize_action(
    uid: str,
    action: str,
    token: Optional[str],
    credentials: Optional[HTTPAuthorizationCredentials],
    db,
) -> None:
    """Authorize an approve/reject action via email token OR admin session."""
    # Try email token first
    if token and verify_approval_token(uid, action, token):
        return

    # Fall back to admin session
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        claims = verify_firebase_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    admin_user = crud.get_user(db, claims["uid"])
    if admin_user is None or admin_user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Admin access required.")
