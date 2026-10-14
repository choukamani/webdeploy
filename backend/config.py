"""
Application configuration — all settings sourced from environment variables.
Uses pydantic-settings for validation, type coercion, and .env file support.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Top-level application settings."""

    # ── GCP ──────────────────────────────────────────────────────────
    PROJECT_ID: str = "your-gcp-project"
    GOOGLE_APPLICATION_CREDENTIALS: str = ""

    # ── Dedicated platform load balancer ("sites LB") ────────────────
    # Every *new* website deployed by the platform (demo, prod, Cloud Run)
    # is routed through this LB. It is provisioned once by
    # ``scripts/provision_sites_lb.py`` and never shared with hand-made sites.
    SITES_LB_PREFIX: str = "webdeploy-sites"
    # Demo sites: path-based (https://{SITES_DEMO_HOST}/{site}/), all served
    # from ONE shared bucket + backend bucket, so a demo site costs no URL map
    # entry and no backend bucket (the two limits that filled up legacy-demo-lb).
    SITES_DEMO_HOST: str = "demo.example.com"
    SITES_DEMO_BUCKET: str = ""  # defaults to "{PROJECT_ID}-webdeploy-demo-sites"
    # Cloud Run apps: host-based (https://{app}.{SITES_APPS_DOMAIN}/) through a
    # serverless NEG + backend service, covered by a wildcard certificate.
    SITES_APPS_DOMAIN: str = "example.com"
    # Cloud DNS zone holding SITES_DEMO_HOST and the app hostnames.
    SITES_DNS_ZONE: str = "example-com"

    # ── Legacy load balancers (read/teardown only for new websites) ──
    # Websites created before the sites LB keep living where they are, so
    # their public URLs never change; deleting them cleans these LBs up.
    DEMO_DOMAIN: str = "example.com"
    DEMO_URL_MAP_NAME: str = "legacy-demo-lb"
    DEMO_HTTPS_PROXY_NAME: str = "legacy-demo-lb-target-proxy"

    PROD_URL_MAP_NAME: str = "websites-urlmap-prod"
    PROD_GLOBAL_IP_NAME: str = "websites-lb-ip-prod"
    PROD_HTTPS_PROXY_NAME: str = "websites-https-proxy-prod"

    # Prod infrastructure toggles
    PROD_AUTO_REGISTER_DOMAINS: bool = False
    PROD_AUTO_CREATE_DNS_ZONE: bool = True
    PROD_AUTO_CREATE_SSL_CERT: bool = True

    # Domain registration (Cloud Domains WHOIS contact)
    DOMAINS_CONTACT_EMAIL: str = ""
    DOMAINS_CONTACT_PHONE: str = ""
    DOMAINS_CONTACT_COMPANY: str = ""
    DOMAINS_CONTACT_COUNTRY: str = "FR"
    DOMAINS_CONTACT_STATE: str = ""
    DOMAINS_CONTACT_CITY: str = ""
    DOMAINS_CONTACT_ADDRESS: str = ""
    DOMAINS_CONTACT_POSTAL_CODE: str = ""
    DOMAINS_LOCATION: str = "global"

    # Bucket
    BUCKET_LOCATION: str = "US"
    BUCKET_CORS_MAX_AGE: int = 3600

    # CDN
    CDN_DEFAULT_TTL: int = 3600
    CDN_MAX_TTL: int = 86400
    CDN_CLIENT_TTL: int = 3600
    CDN_NEGATIVE_CACHING: bool = True
    CDN_NEGATIVE_CACHING_TTL: int = 120

    # ── AI Validation ───────────────────────────────────────────────
    # Providers tried in order; a provider without an API key is skipped.
    AI_PROVIDERS: str = "gemini,openai_compatible,openrouter"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    # Any OpenAI-compatible endpoint: OpenAI, Mistral, Ollama (/v1), vLLM...
    OPENAI_COMPATIBLE_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_COMPATIBLE_API_KEY: str = ""
    OPENAI_COMPATIBLE_MODEL: str = "gpt-4o-mini"
    OPENROUTER_API_KEY: str = ""  # free-tier fallback
    OPENROUTER_MODEL: str = "meta-llama/llama-3.1-8b-instruct:free"
    AI_ALERT_EMAILS: str = ""  # notified when a provider key is invalid or out of credit

    # ── Email (Gmail API via service account) ────────────────────────
    GMAIL_DELEGATED_USER: str = ""
    NOTIFICATION_FROM_EMAIL: str = "webdeploy@example.com"
    NOTIFICATION_TO_EMAILS: str = ""
    DAILY_REPORT_EMAILS: str = ""

    # ── Report scheduling ─────────────────────────────────────────────
    WEEKLY_REPORT_DAY: str = "friday"   # lowercase day name for weekly report
    MONTHLY_REPORT_DAY: int = 1         # day of month for monthly report

    # ── Auth ─────────────────────────────────────────────────────────
    ADMIN_APPROVAL_EMAIL: str = "admin@example.com"
    APPROVAL_TOKEN_SECRET: str = ""
    FRONTEND_URL: str = "http://localhost:3500"
    GOOGLE_CLIENT_ID: str = ""  # OAuth client ID for GIS token verification

    # ── App ──────────────────────────────────────────────────────────
    DEPLOY_UPLOADS_BUCKET: str = ""  # GCS bucket for ZIP backups; defaults to "{PROJECT_ID}-deploy-uploads"
    UPLOAD_DIR: str = "./uploads"
    TEMP_DIR: str = "./tmp"
    LOG_LEVEL: str = "INFO"
    DATABASE_URL: str = "sqlite:///./data/webdeploy.db"

    # ── Build ────────────────────────────────────────────────────────
    BUILD_TIMEOUT_SECONDS: int = 600  # 10 minutes
    PREVIEW_TIMEOUT_SECONDS: int = 30
    MAX_ZIP_SIZE_MB: int = 500
    PIPELINE_MAX_TIMEOUT_SECONDS: int = 900  # 15 minutes — hard timeout for entire pipeline

    # ── Cloud Run ──────────────────────────────────────────────────
    CLOUDRUN_REGION: str = "europe-west1"
    CLOUDRUN_MEMORY: str = "512Mi"
    CLOUDRUN_CPU: str = "1"
    CLOUDRUN_MAX_INSTANCES: int = 10
    CLOUDRUN_MIN_INSTANCES: int = 0
    CLOUDRUN_ARTIFACT_REPO: str = "cloud-run-images"
    CLOUD_BUILD_TIMEOUT_SECONDS: int = 600

    # extra="ignore": a stale key left in someone's .env (settings get renamed
    # and retired over time) must not stop the app from booting.
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # ── Derived helpers ──────────────────────────────────────────────
    @property
    def upload_path(self) -> Path:
        p = Path(self.UPLOAD_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def temp_path(self) -> Path:
        p = Path(self.TEMP_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def deploy_uploads_bucket_name(self) -> str:
        return self.DEPLOY_UPLOADS_BUCKET or f"{self.PROJECT_ID}-deploy-uploads"

    @property
    def sites_demo_bucket_name(self) -> str:
        return self.SITES_DEMO_BUCKET or f"{self.PROJECT_ID}-webdeploy-demo-sites"

    @property
    def notification_emails_list(self) -> list[str]:
        if not self.NOTIFICATION_TO_EMAILS:
            return []
        return [e.strip() for e in self.NOTIFICATION_TO_EMAILS.split(",") if e.strip()]

    @property
    def daily_report_emails_list(self) -> list[str]:
        if not self.DAILY_REPORT_EMAILS:
            return []
        return [e.strip() for e in self.DAILY_REPORT_EMAILS.split(",") if e.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor — import this in other modules.

    Cached: settings come from the environment, which does not change while
    the process runs, and re-parsing them on every request is wasted work.
    """
    return Settings()
