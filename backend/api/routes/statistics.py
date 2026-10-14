"""
Statistics API routes — aggregated pipeline stats and on-demand report trigger.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import zoneinfo
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from config import Settings, get_settings
from db.database import get_db
from db.stats_queries import query_deployments_in_range, compute_statistics
from api.dependencies import require_admin

logger = logging.getLogger("webdeploy.api.statistics")
router = APIRouter(prefix="/api/statistics", tags=["statistics"])

PARIS_TZ = zoneinfo.ZoneInfo("Europe/Paris")


# ═══════════════════════════════════════════════════════════════════════
#  Response models
# ═══════════════════════════════════════════════════════════════════════

class DeployerStats(BaseModel):
    first_name: str = ""
    last_name: str = ""
    name: str
    email: str
    total: int
    with_ai: int
    without_ai: int
    websites: list[str] = []
    modes: list[str] = []
    ai_cost: str = "$0.00"


class DailyBreakdown(BaseModel):
    date: str
    day_name: str = ""
    total: int
    with_ai: int
    without_ai: int


class AITokenStats(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class StatisticsResponse(BaseModel):
    period_start: str
    period_end: str
    total_count: int
    with_ai_count: int
    without_ai_count: int
    average_per_day: float
    per_deployer: list[DeployerStats]
    daily_breakdown: list[DailyBreakdown]
    per_status: dict[str, int] = {}
    ai_tokens: AITokenStats


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/statistics — fetch pipeline statistics
# ═══════════════════════════════════════════════════════════════════════

@router.get("", response_model=StatisticsResponse)
def get_statistics(
    start_date: Optional[str] = Query(
        None, description="Start date YYYY-MM-DD (defaults to 30 days ago)",
    ),
    end_date: Optional[str] = Query(
        None, description="End date YYYY-MM-DD (defaults to today)",
    ),
    preset: Optional[str] = Query(
        None, description="Preset: 'today', '3days', '7days', '30days'",
    ),
    user=Depends(require_admin),
    db=Depends(get_db),
) -> StatisticsResponse:
    """
    Return pipeline statistics for a date range.

    Use ``preset`` for quick access or ``start_date`` / ``end_date`` for
    custom ranges.  Presets take precedence over explicit dates.
    """
    now_paris = datetime.now(PARIS_TZ)
    today = now_paris.date()

    if preset:
        presets = {
            "today": (today, today),
            "3days": (today - timedelta(days=2), today),
            "7days": (today - timedelta(days=6), today),
            "30days": (today - timedelta(days=29), today),
        }
        if preset not in presets:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid preset '{preset}'. Use: {', '.join(presets.keys())}",
            )
        start_d, end_d = presets[preset]
    else:
        try:
            start_d = date.fromisoformat(start_date) if start_date else today - timedelta(days=29)
            end_d = date.fromisoformat(end_date) if end_date else today
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Dates must be in YYYY-MM-DD format.",
            )

    if start_d > end_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must be <= end_date.",
        )

    # Convert to UTC boundaries (Paris timezone, consistent with daily report)
    start_utc = datetime(start_d.year, start_d.month, start_d.day, tzinfo=PARIS_TZ).astimezone(timezone.utc)
    end_utc = (
        datetime(end_d.year, end_d.month, end_d.day, tzinfo=PARIS_TZ)
        + timedelta(days=1)
    ).astimezone(timezone.utc)

    deployments = query_deployments_in_range(db, start_utc, end_utc)
    stats = compute_statistics(deployments)

    return StatisticsResponse(
        period_start=start_d.isoformat(),
        period_end=end_d.isoformat(),
        **stats,
    )


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/statistics/send-report — on-demand report trigger
# ═══════════════════════════════════════════════════════════════════════

class OnDemandReportRequest(BaseModel):
    start_date: str
    end_date: str
    send_to_deployers: bool = True
    send_to_admins: bool = True


class OnDemandReportResponse(BaseModel):
    emails_sent: int
    recipients: list[str]


@router.post("/send-report", response_model=OnDemandReportResponse)
async def send_on_demand_report(
    body: OnDemandReportRequest,
    user=Depends(require_admin),
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OnDemandReportResponse:
    """
    Trigger sending a report for a custom date range.

    - ``send_to_deployers=true`` → each deployer receives a personalized email
    - ``send_to_admins=true`` → configured admin emails get the full summary
    """
    try:
        start_d = date.fromisoformat(body.start_date)
        end_d = date.fromisoformat(body.end_date)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dates must be in YYYY-MM-DD format.",
        )

    if start_d > end_d:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must be <= end_date.",
        )

    start_utc = datetime(start_d.year, start_d.month, start_d.day, tzinfo=PARIS_TZ).astimezone(timezone.utc)
    end_utc = (
        datetime(end_d.year, end_d.month, end_d.day, tzinfo=PARIS_TZ)
        + timedelta(days=1)
    ).astimezone(timezone.utc)

    deployments = query_deployments_in_range(db, start_utc, end_utc)
    stats = compute_statistics(deployments)

    from services.email_service import EmailService
    email_svc = EmailService(settings=settings)

    all_recipients: list[str] = []
    period_label = f"{start_d.isoformat()} au {end_d.isoformat()}"

    # Send full summary to admin emails
    if body.send_to_admins:
        admin_recipients = settings.daily_report_emails_list
        if admin_recipients:
            await email_svc.send_period_report(
                report_type="custom",
                period_label=period_label,
                stats=stats,
                recipients=admin_recipients,
            )
            all_recipients.extend(admin_recipients)

    # Send personalized report to each deployer
    if body.send_to_deployers:
        for deployer in stats["per_deployer"]:
            email = deployer["email"]
            if email and email != "unknown" and email not in all_recipients:
                await email_svc.send_personalized_report(
                    deployer_info=deployer,
                    period_label=period_label,
                    overall_stats=stats,
                )
                all_recipients.append(email)

    return OnDemandReportResponse(
        emails_sent=len(all_recipients),
        recipients=all_recipients,
    )
