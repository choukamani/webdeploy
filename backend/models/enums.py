"""Enumerations used across the application."""

from __future__ import annotations

from enum import Enum


class DeploymentMode(str, Enum):
    DEMO = "demo"
    SUBDOMAIN = "subdomain"
    PROD = "prod"
    CLOUDRUN = "cloudrun"


class DeploymentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class PipelineStep(str, Enum):
    EXTRACT = "EXTRACT"
    AI_INSPECT = "AI_INSPECT"
    AI_FIX = "AI_FIX"
    BUILD = "BUILD"
    VERIFY = "VERIFY"
    DOMAIN_REGISTER = "DOMAIN_REGISTER"
    INFRA = "INFRA"
    UPLOAD = "UPLOAD"
    NOTIFY = "NOTIFY"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class LogLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class UserRole(str, Enum):
    ADMIN = "admin"
    SIMPLE_USER = "simple_user"
    SUPER_USER = "super_user"


class UserStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
