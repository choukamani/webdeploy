"""CRUD operations for deployment and log records using Firestore.

All functions accept a Firestore client as the ``db`` parameter (replacing
the previous SQLAlchemy Session).  Return values use ``SimpleNamespace`` to
preserve attribute-style access (``rec.status``) expected by the rest of
the codebase.
"""

from __future__ import annotations

import itertools
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional, Sequence

from google.cloud.firestore_v1 import Client as FirestoreClient
from google.cloud.firestore_v1.base_query import FieldFilter

from models.enums import DeploymentStatus, LogLevel, PipelineStep, StepStatus, UserStatus

logger = logging.getLogger("webdeploy.crud")

# Collection names
DEPLOYMENTS = "deployments"
_DEPLOYMENTS = DEPLOYMENTS
_LOGS = "deployment_logs"
_USERS = "users"


# ── Helpers ────────────────────────────────────────────────────────────

def _doc_to_record(doc_snapshot) -> SimpleNamespace:
    """Convert a Firestore document snapshot to a namespace with attribute access."""
    data = doc_snapshot.to_dict() or {}
    data["id"] = doc_snapshot.id
    return SimpleNamespace(**data)


# ═══════════════════════════════════════════════════════════════════════
#  Deployment CRUD
# ═══════════════════════════════════════════════════════════════════════

def create_deployment(
    db: FirestoreClient,
    *,
    deployment_id: str,
    website_name: str,
    mode: str,
    domain: Optional[str],
    notification_emails: str,
    zip_filename: str,
    deployer_first_name: str = "",
    deployer_last_name: str = "",
    deployer_email: str = "",
    ai_enabled: bool = False,
    website_key: Optional[str] = None,
) -> SimpleNamespace:
    initial_steps = {step.value: StepStatus.PENDING.value for step in PipelineStep}
    data = {
        "website_name": website_name,
        "mode": mode,
        "domain": domain,
        "status": DeploymentStatus.QUEUED.value,
        "current_step": None,
        "steps_status": json.dumps(initial_steps),
        "result_url": None,
        "ai_summary": None,
        "error_message": None,
        "notification_emails": notification_emails,
        "zip_filename": zip_filename,
        "deployer_first_name": deployer_first_name,
        "deployer_last_name": deployer_last_name,
        "deployer_email": deployer_email,
        "ai_enabled": ai_enabled,
        "ai_token_usage": None,
        "website_key": website_key,
        "created_at": datetime.now(timezone.utc),
        "started_at": None,
        "completed_at": None,
    }
    db.collection(_DEPLOYMENTS).document(deployment_id).set(data)
    data["id"] = deployment_id
    return SimpleNamespace(**data)


def get_deployment(db: FirestoreClient, deployment_id: str) -> Optional[SimpleNamespace]:
    doc = db.collection(_DEPLOYMENTS).document(deployment_id).get()
    if not doc.exists:
        return None
    return _doc_to_record(doc)


def _deployments_query(
    db: FirestoreClient,
    *,
    start_utc: Optional[datetime] = None,
    end_utc: Optional[datetime] = None,
):
    """
    Base deployments query, newest first, optionally bounded by ``created_at``.

    ``start_utc`` is inclusive and ``end_utc`` exclusive — the same convention
    as ``stats_queries.query_deployments_in_range``.  The range and the sort
    both use ``created_at``, so no composite index is required.
    """
    query = db.collection(_DEPLOYMENTS)
    if start_utc is not None:
        query = query.where("created_at", ">=", start_utc)
    if end_utc is not None:
        query = query.where("created_at", "<", end_utc)
    return query.order_by("created_at", direction="DESCENDING")


# Fields the in-Python predicate reads — the projection used when counting.
_FILTER_FIELDS = ["deployer_email", "mode", "status"]


def _record_predicate(
    deployer_email: Optional[str],
    modes: Optional[Sequence[str]],
    statuses: Optional[Sequence[str]],
):
    """
    Build a predicate for the filters that cannot ride along with the
    ``created_at`` range in Firestore without a composite index, or ``None``
    when no such filter is active.
    """
    if deployer_email is None and modes is None and statuses is None:
        return None

    mode_set = set(modes) if modes is not None else None
    status_set = set(statuses) if statuses is not None else None

    def matches(record) -> bool:
        if (
            deployer_email is not None
            and getattr(record, "deployer_email", "") != deployer_email
        ):
            return False
        if mode_set is not None and getattr(record, "mode", "") not in mode_set:
            return False
        if status_set is not None and getattr(record, "status", "") not in status_set:
            return False
        return True

    return matches


def list_deployments(
    db: FirestoreClient,
    limit: int = 100,
    offset: int = 0,
    *,
    start_utc: Optional[datetime] = None,
    end_utc: Optional[datetime] = None,
    deployer_email: Optional[str] = None,
    modes: Optional[Sequence[str]] = None,
    statuses: Optional[Sequence[str]] = None,
) -> list[SimpleNamespace]:
    """
    Return deployments newest-first, optionally restricted to a ``created_at``
    window, a single deployer, a set of modes, and/or a set of statuses.

    Everything except the date range is matched in Python so that no composite
    index is needed.  Limit and offset are therefore applied *after* filtering —
    pushing them down would mean a caller only ever sees whichever matching rows
    happen to land inside the first ``limit`` rows globally.
    """
    query = _deployments_query(db, start_utc=start_utc, end_utc=end_utc)
    predicate = _record_predicate(deployer_email, modes, statuses)

    if predicate is None:
        return [
            _doc_to_record(doc)
            for doc in query.offset(offset).limit(limit).stream()
        ]

    kept = (
        record
        for record in (_doc_to_record(doc) for doc in query.stream())
        if predicate(record)
    )
    return list(itertools.islice(kept, offset, offset + limit))


def count_deployments(
    db: FirestoreClient,
    *,
    start_utc: Optional[datetime] = None,
    end_utc: Optional[datetime] = None,
    deployer_email: Optional[str] = None,
    modes: Optional[Sequence[str]] = None,
    statuses: Optional[Sequence[str]] = None,
) -> int:
    """
    Count deployments matching the same filters as :func:`list_deployments`,
    ignoring limit/offset — lets callers report how many rows exist beyond the
    page they were served.
    """
    query = _deployments_query(db, start_utc=start_utc, end_utc=end_utc)
    predicate = _record_predicate(deployer_email, modes, statuses)

    if predicate is not None:
        return sum(
            1
            for doc in query.select(_FILTER_FIELDS).stream()
            if predicate(_doc_to_record(doc))
        )

    try:
        # Aggregation query — one read instead of one per document.
        return int(query.count().get()[0][0].value)
    except Exception:
        logger.warning(
            "Firestore count() aggregation unavailable; falling back to a scan.",
            exc_info=True,
        )
        return sum(1 for _ in query.select([]).stream())


def delete_deployment(db: FirestoreClient, deployment_id: str) -> bool:
    """Delete a deployment record and its associated logs.

    Logs go first, in batched writes: if the process dies part-way the
    record survives, so the deletion can still be found and finished.
    """
    doc_ref = db.collection(_DEPLOYMENTS).document(deployment_id)
    logs_query = db.collection(_LOGS).where(
        filter=FieldFilter("deployment_id", "==", deployment_id)
    )
    batch, pending = db.batch(), 0
    for log_doc in logs_query.select([]).stream():
        batch.delete(log_doc.reference)
        pending += 1
        if pending == 450:  # Firestore caps a batch at 500 writes
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()

    if not doc_ref.get().exists:
        return False
    doc_ref.delete()
    return True


def update_deployment_status(
    db: FirestoreClient,
    deployment_id: str,
    *,
    status: Optional[str] = None,
    current_step: Optional[str] = None,
    result_url: Optional[str] = None,
    ai_summary: Optional[str] = None,
    error_message: Optional[str] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
) -> None:
    updates = {}
    if status is not None:
        updates["status"] = status
    if current_step is not None:
        updates["current_step"] = current_step
    if result_url is not None:
        updates["result_url"] = result_url
    if ai_summary is not None:
        updates["ai_summary"] = ai_summary
    if error_message is not None:
        updates["error_message"] = error_message
    if started_at is not None:
        updates["started_at"] = started_at
    if completed_at is not None:
        updates["completed_at"] = completed_at

    if updates:
        db.collection(_DEPLOYMENTS).document(deployment_id).update(updates)


def update_step_status(
    db: FirestoreClient,
    deployment_id: str,
    step: str,
    step_status: str,
) -> None:
    doc_ref = db.collection(_DEPLOYMENTS).document(deployment_id)
    doc = doc_ref.get()
    if not doc.exists:
        return

    data = doc.to_dict()
    steps = {}
    raw = data.get("steps_status")
    if raw:
        try:
            steps = json.loads(raw)
        except json.JSONDecodeError:
            steps = {}
    steps[step] = step_status
    doc_ref.update({"steps_status": json.dumps(steps)})


# ═══════════════════════════════════════════════════════════════════════
#  Log CRUD
# ═══════════════════════════════════════════════════════════════════════

def add_log(
    db: FirestoreClient,
    deployment_id: str,
    message: str,
    level: str = LogLevel.INFO.value,
    step: Optional[str] = None,
) -> SimpleNamespace:
    data = {
        "deployment_id": deployment_id,
        "level": level,
        "step": step,
        "message": message,
        "timestamp": datetime.now(timezone.utc),
    }
    doc_ref = db.collection(_LOGS).add(data)
    data["id"] = doc_ref[1].id
    return SimpleNamespace(**data)


def get_logs(db: FirestoreClient, deployment_id: str) -> list[SimpleNamespace]:
    query = (
        db.collection(_LOGS)
        .where("deployment_id", "==", deployment_id)
        .order_by("timestamp")
    )
    return [_doc_to_record(doc) for doc in query.stream()]


# ═══════════════════════════════════════════════════════════════════════
#  User CRUD
# ═══════════════════════════════════════════════════════════════════════

def create_user(
    db: FirestoreClient,
    *,
    uid: str,
    email: str,
    display_name: str = "",
    requested_role: str = "simple_user",
) -> SimpleNamespace:
    """Create a new user document (pending approval). Document ID = Firebase UID."""
    data = {
        "email": email,
        "display_name": display_name,
        "role": None,
        "status": UserStatus.PENDING.value,
        "requested_role": requested_role,
        "created_at": datetime.now(timezone.utc),
        "approved_at": None,
        "approved_by": None,
    }
    db.collection(_USERS).document(uid).set(data)
    data["uid"] = uid
    data["id"] = uid
    return SimpleNamespace(**data)


def get_user(db: FirestoreClient, uid: str) -> Optional[SimpleNamespace]:
    """Get a user by Firebase UID. Returns None if not found."""
    doc = db.collection(_USERS).document(uid).get()
    if not doc.exists:
        return None
    record = _doc_to_record(doc)
    record.uid = doc.id
    return record


def update_user_status(
    db: FirestoreClient,
    uid: str,
    *,
    status: str,
    role: Optional[str] = None,
    approved_by: Optional[str] = None,
) -> None:
    """Update a user's status and optionally assign a role."""
    updates: dict = {"status": status}
    if role is not None:
        updates["role"] = role
    if approved_by is not None:
        updates["approved_by"] = approved_by
    if status == UserStatus.APPROVED.value:
        updates["approved_at"] = datetime.now(timezone.utc)
    db.collection(_USERS).document(uid).update(updates)


def list_users(db: FirestoreClient) -> list[SimpleNamespace]:
    """Return all users ordered by created_at descending."""
    query = (
        db.collection(_USERS)
        .order_by("created_at", direction="DESCENDING")
    )
    results = []
    for doc in query.stream():
        record = _doc_to_record(doc)
        record.uid = doc.id
        results.append(record)
    return results


def update_user_role(db: FirestoreClient, uid: str, role: str) -> None:
    """Change the effective role of a user (admin action)."""
    db.collection(_USERS).document(uid).update({"role": role})


def delete_user(db: FirestoreClient, uid: str) -> None:
    """Permanently delete a user's Firestore document."""
    db.collection(_USERS).document(uid).delete()


def create_user_approved(
    db: FirestoreClient,
    *,
    uid: str,
    email: str,
    display_name: str,
    role: str,
    approved_by: str = "admin",
) -> SimpleNamespace:
    """Create a user already approved with a given role (admin-initiated)."""
    now = datetime.now(timezone.utc)
    data = {
        "email": email,
        "display_name": display_name,
        "role": role,
        "status": UserStatus.APPROVED.value,
        "requested_role": role,
        "created_at": now,
        "approved_at": now,
        "approved_by": approved_by,
    }
    db.collection(_USERS).document(uid).set(data)
    data["uid"] = uid
    data["id"] = uid
    return SimpleNamespace(**data)


# ═══════════════════════════════════════════════════════════════════════
#  Notification Preferences CRUD
# ═══════════════════════════════════════════════════════════════════════

def get_notification_preferences(db: FirestoreClient, uid: str) -> Optional[dict]:
    """Get notification preferences for a user. Returns None if not set."""
    doc = db.collection(_USERS).document(uid).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    return data.get("notification_preferences")


def update_notification_preferences(db: FirestoreClient, uid: str, prefs: dict) -> dict:
    """Update notification preferences for a user (merge with existing)."""
    doc_ref = db.collection(_USERS).document(uid)
    doc = doc_ref.get()
    if not doc.exists:
        return {}

    current = (doc.to_dict() or {}).get("notification_preferences", {}) or {}
    # Merge: only update fields that are provided (not None)
    merged = {**current}
    for key, value in prefs.items():
        if value is not None:
            merged[key] = value

    doc_ref.update({"notification_preferences": merged})
    return merged


def list_users_with_report_preference(
    db: FirestoreClient, frequency: str,
) -> list[SimpleNamespace]:
    """Return all approved users who opted in for reports at the given frequency.

    Uses single-field query + Python filter to avoid composite index requirements.
    """
    query = db.collection(_USERS).where("status", "==", UserStatus.APPROVED.value)
    results = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        prefs = data.get("notification_preferences") or {}
        if prefs.get("report_enabled") is True and prefs.get("report_frequency") == frequency:
            record = _doc_to_record(doc)
            record.uid = doc.id
            results.append(record)
    return results


# ═══════════════════════════════════════════════════════════════════════
#  Git Integration CRUD
# ═══════════════════════════════════════════════════════════════════════

_GIT_CONNECTIONS = "git_connections"
_PUSH_EVENTS = "git_push_events"


def create_git_connection(
    db: FirestoreClient,
    *,
    uid: str,
    user_email: str,
    provider: str,
    repo_url: str,
    repo_name: str,
    branch: str,
    access_token: str,
) -> SimpleNamespace:
    """Create a new Git connection."""
    import secrets
    webhook_secret = secrets.token_hex(20)
    data = {
        "uid": uid,
        "user_email": user_email,
        "provider": provider,
        "repo_url": repo_url,
        "repo_name": repo_name,
        "branch": branch,
        "access_token": access_token,
        "webhook_secret": webhook_secret,
        "created_at": datetime.now(timezone.utc),
    }
    _, doc_ref = db.collection(_GIT_CONNECTIONS).add(data)
    data["id"] = doc_ref.id
    return SimpleNamespace(**data)


def get_git_connection(db: FirestoreClient, connection_id: str) -> Optional[SimpleNamespace]:
    doc = db.collection(_GIT_CONNECTIONS).document(connection_id).get()
    if not doc.exists:
        return None
    return _doc_to_record(doc)


def list_git_connections(db: FirestoreClient, uid: str) -> list[SimpleNamespace]:
    # Single-field query to avoid composite index requirement; sort in Python
    query = db.collection(_GIT_CONNECTIONS).where("uid", "==", uid)
    results = [_doc_to_record(doc) for doc in query.stream()]
    results.sort(key=lambda r: getattr(r, "created_at", None) or datetime.min, reverse=True)
    return results


def update_git_connection_branch(db: FirestoreClient, connection_id: str, branch: str) -> None:
    db.collection(_GIT_CONNECTIONS).document(connection_id).update({"branch": branch})


def delete_git_connection(db: FirestoreClient, connection_id: str) -> None:
    # Delete push events
    events = db.collection(_PUSH_EVENTS).where("connection_id", "==", connection_id)
    for doc in events.stream():
        doc.reference.delete()
    db.collection(_GIT_CONNECTIONS).document(connection_id).delete()


def create_push_event(
    db: FirestoreClient,
    *,
    connection_id: str,
    branch: str,
    commit_sha: str,
    commit_message: str,
    author: str,
) -> SimpleNamespace:
    data = {
        "connection_id": connection_id,
        "branch": branch,
        "commit_sha": commit_sha,
        "commit_message": commit_message,
        "author": author,
        "timestamp": datetime.now(timezone.utc),
        "deployed": False,
        "deployment_id": None,
    }
    _, doc_ref = db.collection(_PUSH_EVENTS).add(data)
    data["id"] = doc_ref.id
    return SimpleNamespace(**data)


def list_push_events(
    db: FirestoreClient, connection_id: str, limit: int = 50,
) -> list[SimpleNamespace]:
    # Single-field query to avoid composite index requirement; sort in Python
    query = db.collection(_PUSH_EVENTS).where("connection_id", "==", connection_id)
    results = [_doc_to_record(doc) for doc in query.stream()]
    results.sort(key=lambda r: getattr(r, "timestamp", None) or datetime.min, reverse=True)
    return results[:limit]


def get_push_event(db: FirestoreClient, event_id: str) -> Optional[SimpleNamespace]:
    doc = db.collection(_PUSH_EVENTS).document(event_id).get()
    if not doc.exists:
        return None
    return _doc_to_record(doc)


def mark_push_event_deployed(db: FirestoreClient, event_id: str, deployment_id: str) -> None:
    db.collection(_PUSH_EVENTS).document(event_id).update({
        "deployed": True,
        "deployment_id": deployment_id,
    })


# ═══════════════════════════════════════════════════════════════════════
#  Quota CRUD
# ═══════════════════════════════════════════════════════════════════════

_QUOTAS = "quotas"


def get_quota(db: FirestoreClient, target_type: str, target_id: str) -> Optional[dict]:
    """Get quota config. target_type is 'role' or 'user', target_id is role name or uid."""
    doc_id = f"{target_type}:{target_id}"
    doc = db.collection(_QUOTAS).document(doc_id).get()
    if not doc.exists:
        return None
    return doc.to_dict().get("config")


def set_quota(db: FirestoreClient, target_type: str, target_id: str, config: dict) -> None:
    doc_id = f"{target_type}:{target_id}"
    db.collection(_QUOTAS).document(doc_id).set({
        "target_type": target_type,
        "target_id": target_id,
        "config": config,
        "updated_at": datetime.now(timezone.utc),
    })


def delete_quota(db: FirestoreClient, target_type: str, target_id: str) -> None:
    doc_id = f"{target_type}:{target_id}"
    db.collection(_QUOTAS).document(doc_id).delete()


_QUOTA_REQUESTS = "quota_requests"


def create_quota_request(
    db: FirestoreClient,
    *,
    uid: str,
    email: str,
    display_name: str,
    role: str,
    current_quota: dict,
    requested_quota: dict,
    reason: str = "",
) -> SimpleNamespace:
    """Create a quota-increase request (pending admin review)."""
    data = {
        "uid": uid,
        "email": email,
        "display_name": display_name,
        "role": role,
        "current_quota": current_quota,
        "requested_quota": requested_quota,
        "reason": reason,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "reviewed_at": None,
        "reviewed_by": None,
        "admin_note": None,
    }
    _, doc_ref = db.collection(_QUOTA_REQUESTS).add(data)
    data["id"] = doc_ref.id
    return SimpleNamespace(**data)


def list_quota_requests(
    db: FirestoreClient,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[SimpleNamespace]:
    """List quota requests, optionally filtered by status."""
    if status is not None:
        query = db.collection(_QUOTA_REQUESTS).where("status", "==", status)
    else:
        query = db.collection(_QUOTA_REQUESTS)
    results = [_doc_to_record(doc) for doc in query.stream()]
    results.sort(key=lambda r: getattr(r, "created_at", None) or datetime.min, reverse=True)
    return results[:limit]


def get_quota_request(db: FirestoreClient, request_id: str) -> Optional[SimpleNamespace]:
    doc = db.collection(_QUOTA_REQUESTS).document(request_id).get()
    if not doc.exists:
        return None
    return _doc_to_record(doc)


def update_quota_request_status(
    db: FirestoreClient,
    request_id: str,
    *,
    status: str,
    reviewed_by: str,
    admin_note: str = "",
) -> None:
    db.collection(_QUOTA_REQUESTS).document(request_id).update({
        "status": status,
        "reviewed_at": datetime.now(timezone.utc),
        "reviewed_by": reviewed_by,
        "admin_note": admin_note,
    })


def count_pending_quota_requests(db: FirestoreClient) -> int:
    query = db.collection(_QUOTA_REQUESTS).where("status", "==", "pending")
    return len(list(query.stream()))


def list_admin_emails(db: FirestoreClient) -> list[str]:
    """Return the email addresses of all approved admin users."""
    query = db.collection(_USERS).where("role", "==", "admin")
    emails: list[str] = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        if data.get("status") == UserStatus.APPROVED.value:
            email = data.get("email")
            if email:
                emails.append(email)
    return emails


def get_user_quota_usage(db: FirestoreClient, uid: str, email: str) -> dict:
    """Get current deployment usage for a user.

    Uses a single-field query on deployer_email and filters in Python
    to avoid composite index requirements.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Fetch all user deployments in one query (single field = no composite index)
    user_query = db.collection(_DEPLOYMENTS).where("deployer_email", "==", email)
    user_docs = list(user_query.stream())

    today_count = 0
    active_count = 0
    total_count = len(user_docs)

    for doc in user_docs:
        data = doc.to_dict()
        status = data.get("status", "")

        # Count active
        if status in ("running", "queued"):
            active_count += 1

        # Count today
        created = data.get("created_at")
        if created and hasattr(created, "timestamp"):
            created_utc = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created
            if created_utc >= today_start:
                today_count += 1

    return {
        "deployments_today": today_count,
        "active_deployments": active_count,
        "total_deployments": total_count,
    }


# ═══════════════════════════════════════════════════════════════════════
#  System Alerts CRUD
# ═══════════════════════════════════════════════════════════════════════

_ALERTS = "system_alerts"


def create_alert(
    db: FirestoreClient,
    *,
    severity: str,
    source: str,
    title: str,
    message: str,
) -> SimpleNamespace:
    data = {
        "severity": severity,
        "source": source,
        "title": title,
        "message": message,
        "resolved": False,
        "unique_key": None,
        "created_at": datetime.now(timezone.utc),
        "resolved_at": None,
    }
    _, doc_ref = db.collection(_ALERTS).add(data)
    data["id"] = doc_ref.id
    return SimpleNamespace(**data)


def list_alerts(
    db: FirestoreClient, resolved: Optional[bool] = None, limit: int = 50,
) -> list[SimpleNamespace]:
    # Avoid composite index requirement: filter first, sort in Python
    if resolved is not None:
        query = db.collection(_ALERTS).where("resolved", "==", resolved)
    else:
        query = db.collection(_ALERTS)
    results = [_doc_to_record(doc) for doc in query.stream()]
    results.sort(key=lambda r: getattr(r, "created_at", None) or datetime.min, reverse=True)
    return results[:limit]


def resolve_alert(db: FirestoreClient, alert_id: str) -> None:
    db.collection(_ALERTS).document(alert_id).update({
        "resolved": True,
        "resolved_at": datetime.now(timezone.utc),
    })


def delete_alert(db: FirestoreClient, alert_id: str) -> None:
    db.collection(_ALERTS).document(alert_id).delete()


def count_unresolved_alerts(db: FirestoreClient) -> int:
    query = db.collection(_ALERTS).where("resolved", "==", False)
    return len(list(query.stream()))
