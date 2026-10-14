"""Pydantic models for user management."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from models.enums import UserRole, UserStatus


class UserCreate(BaseModel):
    requested_role: UserRole


class NotificationPreferences(BaseModel):
    """Per-user notification preferences stored in Firestore."""
    # Deployment notifications (success/failure emails)
    deployment_notifications: bool = True
    # Account status notifications (approval/rejection)
    account_notifications: bool = True
    # Report preferences (admin-relevant but available to all roles)
    report_enabled: bool = False
    # Report frequency: "daily", "weekly", "monthly"
    report_frequency: str = "daily"
    # Custom period (only used when report_frequency == "custom")
    report_custom_start: Optional[str] = None
    report_custom_end: Optional[str] = None


class NotificationPreferencesUpdate(BaseModel):
    """Partial update for notification preferences."""
    deployment_notifications: Optional[bool] = None
    account_notifications: Optional[bool] = None
    report_enabled: Optional[bool] = None
    report_frequency: Optional[str] = None
    report_custom_start: Optional[str] = None
    report_custom_end: Optional[str] = None


class UserResponse(BaseModel):
    uid: str
    email: str
    display_name: str = ""
    role: Optional[str] = None
    status: str = UserStatus.PENDING.value
    requested_role: str = UserRole.SIMPLE_USER.value
    created_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    notification_preferences: Optional[NotificationPreferences] = None

    @classmethod
    def from_record(cls, record) -> "UserResponse":
        # Parse notification preferences from Firestore dict
        raw_prefs = getattr(record, "notification_preferences", None)
        prefs = None
        if isinstance(raw_prefs, dict):
            prefs = NotificationPreferences(**raw_prefs)
        elif isinstance(raw_prefs, NotificationPreferences):
            prefs = raw_prefs

        return cls(
            uid=record.uid if hasattr(record, "uid") else record.id,
            email=getattr(record, "email", ""),
            display_name=getattr(record, "display_name", ""),
            role=getattr(record, "role", None),
            status=getattr(record, "status", UserStatus.PENDING.value),
            requested_role=getattr(record, "requested_role", UserRole.SIMPLE_USER.value),
            created_at=getattr(record, "created_at", None),
            approved_at=getattr(record, "approved_at", None),
            notification_preferences=prefs,
        )
