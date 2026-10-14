"""
Deployment report schedulers — daily, weekly, and monthly.

- **Daily** at 18:00 Europe/Paris: today's stats
- **Weekly** every Friday at 18:00 Europe/Paris: last 7 days
- **Monthly** on 1st of each month at 09:00 Europe/Paris: previous calendar month

All reports are sent automatically to the configured admin emails
(DAILY_REPORT_EMAILS) without any user action.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import zoneinfo

from google.cloud import firestore

from config import get_settings
from db.database import SessionLocal
from db import crud
from db.stats_queries import query_deployments_in_range, compute_statistics

logger = logging.getLogger("webdeploy.report_scheduler")

PARIS_TZ = zoneinfo.ZoneInfo("Europe/Paris")

# Day name mapping for scheduler config
_DAY_NAME_TO_WEEKDAY = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Lock TTL — if a lock is older than this, it's stale and can be overridden
_LOCK_TTL_SECONDS = 3600  # 1 hour


def _acquire_report_lock(lock_id: str) -> bool:
    """Try to acquire a Firestore-based distributed lock for a report.

    Returns True if the lock was acquired (this instance should send the
    report). Returns False if another instance already holds the lock.

    Uses a Firestore transaction to atomically check-and-set, preventing
    multiple Cloud Run instances from sending the same report.
    """
    db = SessionLocal()
    lock_ref = db.collection("report_locks").document(lock_id)

    try:
        @firestore.transactional
        def _try_acquire(transaction):
            doc = lock_ref.get(transaction=transaction)
            if doc.exists:
                data = doc.to_dict()
                locked_at = data.get("locked_at")
                if locked_at:
                    if hasattr(locked_at, "timestamp"):
                        age = (datetime.now(timezone.utc) - locked_at.replace(tzinfo=timezone.utc)).total_seconds()
                    else:
                        age = 0
                    if age < _LOCK_TTL_SECONDS:
                        return False

            transaction.set(lock_ref, {
                "locked_at": datetime.now(timezone.utc),
                "status": "sending",
            })
            return True

        transaction = db.transaction()
        acquired = _try_acquire(transaction)
        if not acquired:
            logger.info("Report lock '%s' held by another instance — skipping.", lock_id)
        return acquired

    except Exception as exc:
        logger.warning("Failed to acquire report lock '%s': %s — proceeding anyway", lock_id, exc)
        return True  # On error, proceed to avoid missing reports


def _release_report_lock(lock_id: str, status: str = "sent") -> None:
    """Mark the lock as completed."""
    try:
        db = SessionLocal()
        lock_ref = db.collection("report_locks").document(lock_id)
        lock_ref.update({"status": status})
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  Daily report scheduler (18:00 Paris)
# ═══════════════════════════════════════════════════════════════════════

async def daily_report_scheduler() -> None:
    """Background task: send daily report at 18:00 Europe/Paris (weekdays only)."""
    while True:
        now = datetime.now(PARIS_TZ)
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)

        # Skip Saturday (5) and Sunday (6)
        while target.weekday() in (5, 6):
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(
            "Daily report scheduler: next run in %.0f seconds (at %s)",
            wait_seconds, target.isoformat(),
        )
        await asyncio.sleep(wait_seconds)

        try:
            lock_id = f"daily_{datetime.now(PARIS_TZ).strftime('%Y-%m-%d')}"
            if _acquire_report_lock(lock_id):
                await _generate_daily_report()
                _release_report_lock(lock_id)
            else:
                logger.info("Daily report already sent by another instance — skipping.")
        except Exception as exc:
            logger.error("Daily report failed: %s", exc, exc_info=True)


def _get_report_recipients(db, frequency: str, fallback_emails: list[str]) -> list[str]:
    """Get report recipients: users who opted in for this frequency + fallback config emails."""
    recipients = set()

    # 1. Users who opted in via notification preferences
    try:
        opted_in_users = crud.list_users_with_report_preference(db, frequency)
        for user in opted_in_users:
            if getattr(user, "email", ""):
                recipients.add(user.email)
    except Exception as exc:
        logger.warning("Failed to query user report preferences: %s — using fallback", exc)

    # 2. Fallback: configured admin emails (if no one opted in)
    if not recipients and fallback_emails:
        recipients.update(fallback_emails)

    return list(recipients)


async def _generate_daily_report() -> None:
    """Query today's deployments and send the daily summary email."""
    settings = get_settings()
    today = datetime.now(PARIS_TZ).date()

    start_utc = datetime(today.year, today.month, today.day, tzinfo=PARIS_TZ).astimezone(timezone.utc)
    end_utc = (datetime(today.year, today.month, today.day, tzinfo=PARIS_TZ) + timedelta(days=1)).astimezone(timezone.utc)

    db = SessionLocal()
    deployments = query_deployments_in_range(db, start_utc, end_utc)
    stats = compute_statistics(deployments)

    logger.info("Daily report: found %d deployment(s) for %s", stats["total_count"], today.isoformat())

    recipients = _get_report_recipients(db, "daily", settings.daily_report_emails_list)
    if not recipients:
        logger.warning("No daily report recipients configured — skipping email")
        return

    from services.email_service import EmailService
    service = EmailService(settings=settings)

    # Use enhanced daily report with AI split
    await service.send_daily_report(
        report_date=today.isoformat(),
        total_deployments=stats["total_count"],
        total_sites=len(set(w for d in stats["per_deployer"] for w in d["websites"])),
        total_deployers=len(stats["per_deployer"]),
        deployers=_convert_deployers_for_daily(stats["per_deployer"]),
        recipients=recipients,
        with_ai_count=stats["with_ai_count"],
        without_ai_count=stats["without_ai_count"],
        average_per_day=stats["average_per_day"],
    )


def _convert_deployers_for_daily(deployers: list[dict]) -> list[dict]:
    """Convert stats_queries deployer format to legacy daily report format."""
    result = []
    for d in deployers:
        result.append({
            "name": d["name"],
            "email": d["email"],
            "count": d["total"],
            "with_ai": d["with_ai"],
            "without_ai": d["without_ai"],
            "sites": ", ".join(d["websites"]),
            "modes": ", ".join(d["modes"]),
            "ai_used": d["with_ai"] > 0,
            "ai_cost": d["ai_cost"],
        })
    return result


# ═══════════════════════════════════════════════════════════════════════
#  Weekly report scheduler (Friday 18:00 Paris by default)
# ═══════════════════════════════════════════════════════════════════════

async def weekly_report_scheduler() -> None:
    """Background task: send weekly report every Friday at 18:00 Europe/Paris."""
    settings = get_settings()
    target_weekday = _DAY_NAME_TO_WEEKDAY.get(settings.WEEKLY_REPORT_DAY.lower(), 4)

    while True:
        now = datetime.now(PARIS_TZ)
        days_ahead = (target_weekday - now.weekday()) % 7
        if days_ahead == 0 and (now.hour > 18 or (now.hour == 18 and now.minute > 0)):
            days_ahead = 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=18, minute=0, second=0, microsecond=0,
        )
        wait_seconds = (target - now).total_seconds()
        if wait_seconds <= 0:
            wait_seconds += 7 * 86400
            target += timedelta(days=7)
        logger.info(
            "Weekly report scheduler: next run in %.0f seconds (at %s)",
            wait_seconds, target.isoformat(),
        )
        await asyncio.sleep(wait_seconds)

        try:
            lock_id = f"weekly_{target.strftime('%Y-%m-%d')}"
            if _acquire_report_lock(lock_id):
                await _generate_weekly_report()
                _release_report_lock(lock_id)
            else:
                logger.info("Weekly report already sent by another instance — skipping.")
        except Exception as exc:
            logger.error("Weekly report failed: %s", exc, exc_info=True)


async def _generate_weekly_report() -> None:
    """Query last 7 days and send weekly summary."""
    settings = get_settings()
    today = datetime.now(PARIS_TZ).date()
    start = today - timedelta(days=6)

    start_utc = datetime(start.year, start.month, start.day, tzinfo=PARIS_TZ).astimezone(timezone.utc)
    end_utc = (datetime(today.year, today.month, today.day, tzinfo=PARIS_TZ) + timedelta(days=1)).astimezone(timezone.utc)

    db = SessionLocal()
    deployments = query_deployments_in_range(db, start_utc, end_utc)
    stats = compute_statistics(deployments)

    logger.info("Weekly report: found %d deployment(s) for %s to %s", stats["total_count"], start, today)

    recipients = _get_report_recipients(db, "weekly", settings.daily_report_emails_list)
    if not recipients:
        return

    from services.email_service import EmailService
    service = EmailService(settings=settings)
    await service.send_period_report(
        report_type="weekly",
        period_label=f"{start.isoformat()} au {today.isoformat()}",
        stats=stats,
        recipients=recipients,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Monthly report scheduler (1st of month 09:00 Paris)
# ═══════════════════════════════════════════════════════════════════════

async def monthly_report_scheduler() -> None:
    """Background task: send monthly report on 1st of each month at 09:00 Europe/Paris."""
    while True:
        now = datetime.now(PARIS_TZ)

        # Target: 1st of next month at 09:00
        if now.month == 12:
            target = now.replace(year=now.year + 1, month=1, day=1, hour=9, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(month=now.month + 1, day=1, hour=9, minute=0, second=0, microsecond=0)

        # If it's the 1st and before 09:00, send today
        if now.day == 1 and now.hour < 9:
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)

        wait_seconds = (target - now).total_seconds()
        logger.info(
            "Monthly report scheduler: next run in %.0f seconds (at %s)",
            wait_seconds, target.isoformat(),
        )
        await asyncio.sleep(wait_seconds)

        try:
            lock_id = f"monthly_{target.strftime('%Y-%m')}"
            if _acquire_report_lock(lock_id):
                await _generate_monthly_report()
                _release_report_lock(lock_id)
            else:
                logger.info("Monthly report already sent by another instance — skipping.")
        except Exception as exc:
            logger.error("Monthly report failed: %s", exc, exc_info=True)


async def _generate_monthly_report() -> None:
    """Query previous calendar month and send monthly summary."""
    settings = get_settings()
    today = datetime.now(PARIS_TZ).date()

    # Previous month boundaries
    first_of_current = date(today.year, today.month, 1)
    last_of_prev = first_of_current - timedelta(days=1)
    first_of_prev = date(last_of_prev.year, last_of_prev.month, 1)

    start_utc = datetime(first_of_prev.year, first_of_prev.month, first_of_prev.day, tzinfo=PARIS_TZ).astimezone(timezone.utc)
    end_utc = datetime(first_of_current.year, first_of_current.month, first_of_current.day, tzinfo=PARIS_TZ).astimezone(timezone.utc)

    db = SessionLocal()
    deployments = query_deployments_in_range(db, start_utc, end_utc)
    stats = compute_statistics(deployments)

    month_name = first_of_prev.strftime("%B %Y")
    logger.info("Monthly report: found %d deployment(s) for %s", stats["total_count"], month_name)

    recipients = _get_report_recipients(db, "monthly", settings.daily_report_emails_list)
    if not recipients:
        return

    from services.email_service import EmailService
    service = EmailService(settings=settings)
    await service.send_period_report(
        report_type="monthly",
        period_label=month_name,
        stats=stats,
        recipients=recipients,
    )
