"""Pydantic schemas and SQLAlchemy ORM models for deployments."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase

from models.enums import DeploymentMode, DeploymentStatus, PipelineStep, StepStatus


# ═══════════════════════════════════════════════════════════════════════
#  SQLAlchemy ORM Models
# ═══════════════════════════════════════════════════════════════════════

class Base(DeclarativeBase):
    pass


class DeploymentRecord(Base):
    __tablename__ = "deployments"

    id = Column(String, primary_key=True)
    website_name = Column(String, nullable=False)
    mode = Column(String, nullable=False)
    domain = Column(String, nullable=True)
    status = Column(String, nullable=False, default=DeploymentStatus.QUEUED.value)
    current_step = Column(String, nullable=True)
    steps_status = Column(Text, nullable=True)  # JSON string
    result_url = Column(String, nullable=True)
    ai_summary = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    notification_emails = Column(String, nullable=True)
    zip_filename = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class DeploymentLogRecord(Base):
    __tablename__ = "deployment_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(String, nullable=False, index=True)
    timestamp = Column(DateTime, server_default=func.now())
    level = Column(String, default="INFO")
    step = Column(String, nullable=True)
    message = Column(Text, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
#  Pydantic Schemas
# ═══════════════════════════════════════════════════════════════════════

class DeploymentCreate(BaseModel):
    """Incoming deployment request (from API form)."""
    website_name: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
    mode: DeploymentMode
    domain: Optional[str] = None
    notification_emails: str = ""


class DeploymentConfig(BaseModel):
    """Internal config passed through the pipeline."""
    mode: DeploymentMode
    website_name: str
    domain: Optional[str] = None
    notification_emails: list[str] = []
    deployer_first_name: str = ""
    deployer_last_name: str = ""
    deployer_email: str = ""
    ai_enabled: bool = False
    domain_purchase_confirmed: bool = False


class StepInfo(BaseModel):
    name: str
    status: StepStatus = StepStatus.PENDING
    error: Optional[str] = None
    duration_seconds: Optional[float] = None


class DeploymentResponse(BaseModel):
    """API response for a single deployment."""
    id: str
    website_name: str
    mode: str
    domain: Optional[str]
    status: str
    current_step: Optional[str]
    steps_status: dict[str, str] = {}
    result_url: Optional[str]
    ai_summary: Optional[str]
    error_message: Optional[str]
    notification_emails: Optional[str]
    zip_filename: Optional[str]
    deployer_first_name: Optional[str] = None
    deployer_last_name: Optional[str] = None
    deployer_email: Optional[str] = None
    ai_enabled: bool = False
    ai_token_usage: Optional[str] = None
    dns_nameservers: list[str] = []
    storage_bucket: Optional[str] = None
    backend_bucket: Optional[str] = None
    infra_details: Optional[dict] = None
    website_key: Optional[str] = None
    placement: Optional[str] = None
    # Set while the owning website is being deleted, or when that failed.
    deletion_status: Optional[str] = None
    deletion_error: Optional[str] = None
    created_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    @classmethod
    def from_record(cls, rec: DeploymentRecord) -> DeploymentResponse:
        steps = {}
        if rec.steps_status:
            try:
                steps = json.loads(rec.steps_status)
            except json.JSONDecodeError:
                pass
        return cls(
            id=rec.id,
            website_name=rec.website_name,
            mode=rec.mode,
            domain=rec.domain,
            status=rec.status,
            current_step=rec.current_step,
            steps_status=steps,
            result_url=rec.result_url,
            ai_summary=rec.ai_summary,
            error_message=rec.error_message,
            notification_emails=rec.notification_emails,
            zip_filename=rec.zip_filename,
            deployer_first_name=getattr(rec, "deployer_first_name", None),
            deployer_last_name=getattr(rec, "deployer_last_name", None),
            deployer_email=getattr(rec, "deployer_email", None),
            ai_enabled=getattr(rec, "ai_enabled", False),
            ai_token_usage=getattr(rec, "ai_token_usage", None),
            dns_nameservers=getattr(rec, "dns_nameservers", []) or [],
            storage_bucket=getattr(rec, "storage_bucket", None),
            backend_bucket=getattr(rec, "backend_bucket", None),
            infra_details=getattr(rec, "infra_details", None),
            website_key=getattr(rec, "website_key", None),
            placement=(getattr(rec, "infra_details", None) or {}).get("placement"),
            deletion_status=getattr(rec, "deletion_status", None),
            deletion_error=getattr(rec, "deletion_error", None),
            created_at=rec.created_at,
            started_at=rec.started_at,
            completed_at=rec.completed_at,
        )


class DeploymentCreateResponse(BaseModel):
    """Returned immediately after POST /api/deploy."""
    deployment_id: str
    status: str = "queued"


class PipelineContext(BaseModel):
    """Mutable state carried through the pipeline steps."""
    deployment_id: str
    zip_path: str
    config: DeploymentConfig
    source_path: Optional[str] = None
    dist_path: Optional[str] = None
    vite_config_path: Optional[str] = None
    package_json: Optional[dict] = None
    has_router: bool = False
    is_static: bool = False
    main_html_file: str = "index.html"
    ai_summary: Optional[str] = None
    result_url: Optional[str] = None
    bucket_name: Optional[str] = None
    project_type: Optional[str] = None
    docker_image_uri: Optional[str] = None
    cloudrun_service_name: Optional[str] = None
    ai_token_usage: dict = {}
    dns_nameservers: list[str] = []
    website_key: Optional[str] = None
    upload_prefix: str = ""
    cdn_url_map: Optional[str] = None
    cdn_host: Optional[str] = None
    cdn_path: Optional[str] = None
    infra_resources: list[dict] = []

    model_config = {"arbitrary_types_allowed": True}


class ZipProcessingResult(BaseModel):
    source_path: str
    dist_path: str
    vite_config_path: Optional[str] = None
    package_json: dict = {}
    has_router: bool = False
    is_static: bool = False
    main_html_file: str = "index.html"
    detected_issues: list[str] = []


class AIValidationResult(BaseModel):
    status: str  # "pass" or "needs_fixes"
    issues_found: list[dict] = []
    fixes: list[dict] = []
    summary: str = ""
    token_usage: dict = {}


class DeploymentResult(BaseModel):
    mode: str
    website_name: str
    success: bool = False
    url: Optional[str] = None
    storage_bucket: Optional[str] = None
    backend_bucket: Optional[str] = None
    url_map_updated: bool = False
    cloudrun_service: Optional[str] = None
    docker_image: Optional[str] = None
    dns_nameservers: list[str] = []
    error: Optional[str] = None
    # Where the site's files go: object prefix inside ``storage_bucket``
    # ("{site}/" for path-routed demo sites, "" for a dedicated bucket).
    upload_prefix: str = ""
    placement: Optional[str] = None
    lb_name: Optional[str] = None
    # CDN invalidation target after an upload.
    cdn_url_map: Optional[str] = None
    cdn_host: Optional[str] = None
    cdn_path: Optional[str] = None
    # Informational inventory of what backs the site (shown in the UI).
    resources: list[dict] = []


class LogEntry(BaseModel):
    timestamp: datetime
    level: str
    step: Optional[str]
    message: str
