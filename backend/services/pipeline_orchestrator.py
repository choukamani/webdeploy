"""
Pipeline orchestrator — drives the full deployment workflow step by step.

Each step updates the database, broadcasts progress via WebSocket, and
handles failures gracefully.  The NOTIFY step always runs, even when a
prior step has failed, so the user always receives an email notification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from config import Settings
from db.database import SessionLocal
from db import crud
from models.enums import (
    DeploymentMode,
    DeploymentStatus,
    LogLevel,
    PipelineStep,
    StepStatus,
)
from models.deployment import DeploymentConfig, PipelineContext
from api.dependencies import get_log_callback

logger = logging.getLogger("webdeploy.orchestrator")

# ── Lazy imports for service classes ──────────────────────────────────
# Imported at call-time so the module can be loaded even when heavy
# GCP libraries are not installed in the environment.

_PIPELINE_STEPS = [
    PipelineStep.EXTRACT,
    PipelineStep.AI_INSPECT,
    PipelineStep.AI_FIX,
    PipelineStep.BUILD,
    PipelineStep.VERIFY,
    PipelineStep.DOMAIN_REGISTER,
    PipelineStep.INFRA,
    PipelineStep.UPLOAD,
    PipelineStep.NOTIFY,
]


class PipelineOrchestrator:
    """Orchestrates the full deployment pipeline with real-time logging."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ── Public entry point ────────────────────────────────────────────

    async def run(
        self,
        deployment_id: str,
        zip_path: str,
        config: DeploymentConfig,
    ) -> None:
        """
        Execute all pipeline steps sequentially for a single deployment.

        Wraps the entire pipeline in an asyncio timeout so it can never
        hang forever.  On success the deployment record is updated with
        the result URL.  On failure the error is recorded and a notification
        is sent.  Temporary files are always cleaned up in ``finally``.
        """
        timeout = self._settings.PIPELINE_MAX_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(
                self._run_pipeline(deployment_id, zip_path, config),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Pipeline %s timed out after %ds", deployment_id, timeout,
            )
            db = SessionLocal()
            crud.update_deployment_status(
                db,
                deployment_id,
                status=DeploymentStatus.FAILED.value,
                error_message="Le déploiement a pris trop de temps et a été arrêté automatiquement. Veuillez relancer le déploiement.",
                completed_at=datetime.now(timezone.utc),
            )
            log_cb = get_log_callback(deployment_id)
            log_cb(
                f"Pipeline timed out after {timeout}s",
                level=LogLevel.ERROR.value,
            )

    async def _run_pipeline(
        self,
        deployment_id: str,
        zip_path: str,
        config: DeploymentConfig,
    ) -> None:
        """Inner pipeline logic, wrapped by run() with a timeout guard."""
        from infra.layout import website_key

        log_cb = get_log_callback(deployment_id)
        ctx = PipelineContext(
            deployment_id=deployment_id,
            zip_path=zip_path,
            config=config,
            website_key=website_key(
                config.mode.value, config.website_name, config.domain, self._settings,
            ),
        )

        failed_step: Optional[PipelineStep] = None
        failure_error: Optional[str] = None

        db = SessionLocal()
        try:
            # Mark deployment as RUNNING
            crud.update_deployment_status(
                db,
                deployment_id,
                status=DeploymentStatus.RUNNING.value,
                started_at=datetime.now(timezone.utc),
            )
            log_cb("Pipeline started", level=LogLevel.INFO.value, step=None)

            for step in _PIPELINE_STEPS:
                # ── NOTIFY always runs, even after failure ────────
                if step == PipelineStep.NOTIFY:
                    await self._execute_step(
                        db, ctx, step, log_cb, failed_step=failed_step, failure_error=failure_error,
                    )
                    continue

                # If a prior step already failed, skip remaining work steps
                if failed_step is not None:
                    crud.update_step_status(
                        db, deployment_id, step.value, StepStatus.SKIPPED.value,
                    )
                    continue

                # ── DOMAIN_REGISTER: skip unless prod + confirmed + enabled ──
                if step == PipelineStep.DOMAIN_REGISTER:
                    is_prod = ctx.config.mode == DeploymentMode.PROD
                    auto_register = self._settings.PROD_AUTO_REGISTER_DOMAINS
                    confirmed = ctx.config.domain_purchase_confirmed
                    if not (is_prod and auto_register and confirmed):
                        crud.update_step_status(
                            db, deployment_id, step.value, StepStatus.SKIPPED.value,
                        )
                        log_cb(
                            "Achat de domaine non requis — étape ignorée",
                            level=LogLevel.INFO.value, step=step.value,
                        )
                        continue

                # ── AI_INSPECT / AI_FIX: skip when AI is disabled ────────
                if step in (PipelineStep.AI_INSPECT, PipelineStep.AI_FIX):
                    if not ctx.config.ai_enabled:
                        crud.update_step_status(
                            db, deployment_id, step.value, StepStatus.SKIPPED.value,
                        )
                        log_cb(
                            "AI désactivé — étape ignorée",
                            level=LogLevel.INFO.value, step=step.value,
                        )
                        continue

                try:
                    await self._execute_step(db, ctx, step, log_cb)

                except Exception as exc:
                    failed_step = step
                    failure_error = str(exc)
                    error_msg = f"Step {step.value} failed: {failure_error}"
                    logger.exception(error_msg)
                    log_cb(error_msg, level=LogLevel.ERROR.value, step=step.value)
                    crud.add_log(
                        db, deployment_id, error_msg,
                        level=LogLevel.ERROR.value, step=step.value,
                    )
                    crud.update_step_status(
                        db, deployment_id, step.value, StepStatus.FAILED.value,
                    )
                    crud.update_deployment_status(
                        db, deployment_id, error_message=failure_error,
                    )

            # ── Final status ──────────────────────────────────────
            if failed_step is not None:
                crud.update_deployment_status(
                    db,
                    deployment_id,
                    status=DeploymentStatus.FAILED.value,
                    completed_at=datetime.now(timezone.utc),
                )
                log_cb(
                    f"Pipeline FAILED at step {failed_step.value}",
                    level=LogLevel.ERROR.value,
                )
            else:
                crud.update_deployment_status(
                    db,
                    deployment_id,
                    status=DeploymentStatus.SUCCESS.value,
                    result_url=ctx.result_url,
                    ai_summary=ctx.ai_summary,
                    completed_at=datetime.now(timezone.utc),
                )
                try:
                    from services import website_service
                    website_service.mark_deployed(
                        db, ctx.website_key, deployment_id=deployment_id,
                        url=ctx.result_url, resources=ctx.infra_resources,
                    )
                except Exception:
                    # The site is live either way; the pointer is informational.
                    logger.warning("Could not update website %s", ctx.website_key, exc_info=True)
                log_cb(
                    f"Pipeline completed successfully — URL: {ctx.result_url}",
                    level=LogLevel.INFO.value,
                )

        except Exception as exc:
            # Catch-all for unexpected errors outside the step loop
            logger.exception("Unexpected orchestrator error for %s", deployment_id)
            crud.update_deployment_status(
                db,
                deployment_id,
                status=DeploymentStatus.FAILED.value,
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc),
            )
            log_cb(f"Unexpected error: {exc}", level=LogLevel.ERROR.value)

        finally:
            # Firestore client doesn't need explicit closing
            # ── Clean up temp files ───────────────────────────────
            self._cleanup(ctx, log_cb)

    # ── Step dispatcher ───────────────────────────────────────────────

    async def _execute_step(
        self,
        db,
        ctx: PipelineContext,
        step: PipelineStep,
        log_cb: Callable,
        *,
        failed_step: Optional[PipelineStep] = None,
        failure_error: Optional[str] = None,
    ) -> None:
        """Run a single pipeline step with DB bookkeeping."""
        deployment_id = ctx.deployment_id
        step_name = step.value

        log_cb(f"Starting step: {step_name}", level=LogLevel.INFO.value, step=step_name)
        crud.update_step_status(db, deployment_id, step_name, StepStatus.RUNNING.value)
        crud.update_deployment_status(db, deployment_id, current_step=step_name)
        crud.add_log(db, deployment_id, f"Starting step: {step_name}", step=step_name)

        start = time.monotonic()

        step_map = {
            PipelineStep.EXTRACT: self._step_extract,
            PipelineStep.AI_INSPECT: self._step_ai_inspect,
            PipelineStep.AI_FIX: self._step_ai_fix,
            PipelineStep.BUILD: self._step_build,
            PipelineStep.VERIFY: self._step_verify,
            PipelineStep.DOMAIN_REGISTER: self._step_domain_register,
            PipelineStep.INFRA: self._step_infra,
            PipelineStep.UPLOAD: self._step_upload,
            PipelineStep.NOTIFY: self._step_notify,
        }

        handler = step_map[step]

        if step == PipelineStep.NOTIFY:
            await handler(ctx, log_cb, failed_step=failed_step, failure_error=failure_error)
        else:
            await handler(ctx, log_cb)

        elapsed = round(time.monotonic() - start, 2)
        crud.update_step_status(db, deployment_id, step_name, StepStatus.COMPLETED.value)
        crud.add_log(
            db, deployment_id,
            f"Step {step_name} completed in {elapsed}s",
            step=step_name,
        )
        log_cb(
            f"Step {step_name} completed in {elapsed}s",
            level=LogLevel.INFO.value,
            step=step_name,
        )

    # ── Individual step implementations ───────────────────────────────

    async def _step_extract(self, ctx: PipelineContext, log_cb: Callable) -> None:
        from services.zip_processor import ZipProcessingService

        service = ZipProcessingService(log_callback=log_cb)

        if ctx.config.mode == DeploymentMode.CLOUDRUN:
            result = await asyncio.to_thread(
                service.process_generic, ctx.zip_path, str(self._settings.temp_path),
            )
        else:
            # Try Vite project first; fall back to static HTML/CSS/JS
            try:
                result = await asyncio.to_thread(
                    service.process, ctx.zip_path, str(self._settings.temp_path),
                )
            except ValueError:
                log_cb(
                    "No Vite project detected — trying static HTML/CSS/JS mode",
                    level=LogLevel.INFO.value,
                    step=PipelineStep.EXTRACT.value,
                )
                result = await asyncio.to_thread(
                    service.process_static, ctx.zip_path, str(self._settings.temp_path),
                )

        ctx.source_path = result.source_path
        ctx.dist_path = result.dist_path
        ctx.vite_config_path = result.vite_config_path
        ctx.package_json = result.package_json
        ctx.has_router = result.has_router
        ctx.is_static = result.is_static
        ctx.main_html_file = result.main_html_file

    async def _step_ai_inspect(self, ctx: PipelineContext, log_cb: Callable) -> None:
        if ctx.is_static:
            log_cb(
                "Static HTML site — skipping AI inspection",
                level=LogLevel.INFO.value,
                step=PipelineStep.AI_INSPECT.value,
            )
            ctx.ai_summary = "Static HTML/CSS/JS site — no AI inspection needed."
            return

        from services.ai_validator import AIValidationService

        service = AIValidationService(
            log_callback=log_cb,
            settings=self._settings,
        )
        result = await asyncio.to_thread(
            service.validate_and_fix,
            source_path=ctx.source_path,
            website_name=ctx.config.website_name,
            mode=ctx.config.mode,
            has_router=ctx.has_router,
        )
        ctx.ai_summary = result.summary

        # Store AI token usage for cost tracking
        if result.token_usage:
            ctx.ai_token_usage = result.token_usage
            db = SessionLocal()
            db.collection("deployments").document(ctx.deployment_id).update({
                "ai_token_usage": json.dumps(ctx.ai_token_usage),
            })

    async def _step_ai_fix(self, ctx: PipelineContext, log_cb: Callable) -> None:
        if ctx.is_static:
            log_cb(
                "Static HTML site — skipping AI fix",
                level=LogLevel.INFO.value,
                step=PipelineStep.AI_FIX.value,
            )
            return

        # AI_FIX is combined with AI_INSPECT — the validate_and_fix call
        # already applies any fixes.  Mark as completed (the dispatcher
        # handles the status update).
        log_cb(
            "AI_FIX combined with AI_INSPECT — no additional action required",
            level=LogLevel.INFO.value,
            step=PipelineStep.AI_FIX.value,
        )

    async def _step_build(self, ctx: PipelineContext, log_cb: Callable) -> None:
        if ctx.is_static:
            log_cb(
                "Static HTML site — skipping build (no npm install/build needed)",
                level=LogLevel.INFO.value,
                step=PipelineStep.BUILD.value,
            )
            return

        if ctx.config.mode == DeploymentMode.CLOUDRUN:
            await self._step_build_cloudrun(ctx, log_cb)
            return

        from services.build_service import BuildService

        service = BuildService(
            log_callback=log_cb,
            settings=self._settings,
        )
        await asyncio.to_thread(service.install_dependencies, ctx.source_path)
        dist_path = await asyncio.to_thread(
            service.build,
            ctx.source_path,
            ctx.config.website_name,
            ctx.config.mode,
        )
        if dist_path:
            ctx.dist_path = dist_path

    async def _step_build_cloudrun(self, ctx: PipelineContext, log_cb: Callable) -> None:
        """Cloud Run BUILD step: detect project type and generate Dockerfile."""
        from services.dockerfile_generator import DockerfileGenerator

        generator = DockerfileGenerator(log_callback=log_cb)
        project_type, dockerfile_content = await asyncio.to_thread(
            generator.detect_and_generate, ctx.source_path,
            fallback_to_static=True,
        )

        ctx.project_type = project_type
        log_cb(
            f"Project type: {project_type} — Dockerfile generated",
            level=LogLevel.INFO.value,
            step=PipelineStep.BUILD.value,
        )

        # Compute image URI
        region = self._settings.CLOUDRUN_REGION
        repo = self._settings.CLOUDRUN_ARTIFACT_REPO
        project_id = self._settings.PROJECT_ID
        sname = ctx.config.website_name
        image_tag = ctx.deployment_id[:8]
        image_uri = f"{region}-docker.pkg.dev/{project_id}/{repo}/{sname}:{image_tag}"

        ctx.docker_image_uri = image_uri
        ctx.cloudrun_service_name = sname
        log_cb(
            f"Target image: {image_uri}",
            level=LogLevel.INFO.value,
            step=PipelineStep.BUILD.value,
        )

    async def _step_verify(self, ctx: PipelineContext, log_cb: Callable) -> None:
        if ctx.is_static:
            # For static sites, verify main HTML file exists in dist_path
            import os
            html_path = os.path.join(ctx.dist_path, ctx.main_html_file)
            if not os.path.isfile(html_path):
                raise RuntimeError(f"{ctx.main_html_file} not found at {ctx.dist_path}")
            log_cb(
                f"Static HTML site — verified {ctx.main_html_file} exists",
                level=LogLevel.INFO.value,
                step=PipelineStep.VERIFY.value,
            )
            return

        if ctx.config.mode == DeploymentMode.CLOUDRUN:
            await self._step_verify_cloudrun(ctx, log_cb)
            return

        from services.build_service import BuildService

        service = BuildService(
            log_callback=log_cb,
            settings=self._settings,
        )
        await asyncio.to_thread(
            service.verify_preview,
            ctx.source_path,
            ctx.config.website_name,
            ctx.config.mode,
        )

    async def _step_verify_cloudrun(self, ctx: PipelineContext, log_cb: Callable) -> None:
        """Cloud Run VERIFY step: build Docker image via Cloud Build."""
        from services.cloud_build_service import CloudBuildService

        service = CloudBuildService(settings=self._settings, log_callback=log_cb)
        image_uri = await service.build_image(
            source_path=ctx.source_path,
            image_uri=ctx.docker_image_uri,
        )
        ctx.docker_image_uri = image_uri
        log_cb(
            f"Docker image built successfully: {image_uri}",
            level=LogLevel.INFO.value,
            step=PipelineStep.VERIFY.value,
        )

    async def _step_domain_register(self, ctx: PipelineContext, log_cb: Callable) -> None:
        """Purchase a domain via Cloud Domains API."""
        from services.domain_service import DomainService

        domain = ctx.config.domain
        if not domain:
            raise RuntimeError("Aucun domaine spécifié pour l'achat.")

        log_cb(
            f"Achat du domaine {domain} en cours...",
            level=LogLevel.INFO.value,
            step=PipelineStep.DOMAIN_REGISTER.value,
        )

        service = DomainService(self._settings)
        result = await asyncio.to_thread(service.register_domain, domain)

        if not result.success:
            raise RuntimeError(result.message)

        log_cb(
            result.message,
            level=LogLevel.INFO.value,
            step=PipelineStep.DOMAIN_REGISTER.value,
        )

    async def _step_infra(self, ctx: PipelineContext, log_cb: Callable) -> None:
        """Provision (or reuse) the website's infrastructure.

        The website document decides *where* the site lives: sites created
        before the dedicated sites LB stay on their legacy LB (redeploying
        them must not change a URL that has been shared), everything else
        goes to the sites LB.
        """
        from infra.gcp_helpers import get_credentials
        from infra.layout import Placement
        from services import website_service
        from googleapiclient import discovery

        settings = self._settings
        cfg = ctx.config
        mode = cfg.mode.value
        step = PipelineStep.INFRA.value

        def _log(message: str) -> None:
            log_cb(message, level=LogLevel.INFO.value, step=step)

        async def _async_log(message: str) -> None:
            _log(message)

        def _resolve():
            compute = discovery.build(
                "compute", "v1", cache_discovery=False,
                credentials=get_credentials(settings.GOOGLE_APPLICATION_CREDENTIALS),
            )
            return website_service.resolve_new_placement(
                compute, settings, mode, cfg.website_name, cfg.domain or "",
            )

        if cfg.mode not in (DeploymentMode.DEMO, DeploymentMode.PROD, DeploymentMode.CLOUDRUN):
            # e.g. a stale re-run of a retired "subdomain" deployment — reject
            # before a website document is created for it.
            raise RuntimeError(f"Mode '{mode}' can no longer be deployed")

        website = await asyncio.to_thread(
            website_service.claim_for_deploy,
            SessionLocal(), settings,
            key=ctx.website_key, mode=mode, website_name=cfg.website_name,
            domain=cfg.domain, deployment_id=ctx.deployment_id,
            deployer_email=cfg.deployer_email, resolve_placement=_resolve,
        )
        placement = Placement(website["placement"])
        _log(f"[INFRA] Website {ctx.website_key} — placement: {placement.value}")

        run_app_url = None
        if cfg.mode == DeploymentMode.DEMO and placement == Placement.LEGACY_DEMO:
            from infra.demo_deployer import DemoDeployer
            result = await DemoDeployer(config=settings, log_callback=_async_log).deploy(
                website_name=cfg.website_name, main_html_file=ctx.main_html_file,
            )
            result.upload_prefix = f"{cfg.website_name}/"
            result.cdn_url_map, result.cdn_path = settings.DEMO_URL_MAP_NAME, f"/{cfg.website_name}/*"
            result.resources = [
                {"kind": "gcs_bucket", "name": result.storage_bucket},
                {"kind": "backend_bucket", "name": result.backend_bucket},
                {"kind": "path_rule", "name": f"/{cfg.website_name}/*", "url_map": settings.DEMO_URL_MAP_NAME},
            ]

        elif cfg.mode == DeploymentMode.PROD and placement == Placement.LEGACY_PROD:
            from infra.prod_deployer import ProdDeployer
            result = await ProdDeployer(config=settings, log_callback=_async_log).deploy(
                website_name=cfg.website_name, domain=cfg.domain, main_html_file=ctx.main_html_file,
            )
            result.cdn_url_map, result.cdn_host, result.cdn_path = (
                settings.PROD_URL_MAP_NAME, cfg.domain, "/*",
            )
            result.resources = [
                {"kind": "gcs_bucket", "name": result.storage_bucket},
                {"kind": "backend_bucket", "name": result.backend_bucket},
                {"kind": "host_rule", "name": cfg.domain, "url_map": settings.PROD_URL_MAP_NAME},
            ]

        elif cfg.mode == DeploymentMode.DEMO:
            from infra.sites_lb import SitesLB
            result = await asyncio.to_thread(
                lambda: SitesLB(settings, log=_log).deploy_demo(cfg.website_name)
            )

        elif cfg.mode == DeploymentMode.PROD:
            from infra.sites_lb import SitesLB
            result = await asyncio.to_thread(
                lambda: SitesLB(settings, log=_log).deploy_prod(cfg.website_name, cfg.domain)
            )

        elif cfg.mode == DeploymentMode.CLOUDRUN:
            from infra.cloudrun_deployer import CloudRunDeployer
            from infra.sites_lb import SitesLB
            result = await CloudRunDeployer(config=settings, log_callback=_async_log).deploy(
                website_name=cfg.website_name, image_uri=ctx.docker_image_uri,
            )
            if result.success:
                run_app_url = result.url
                exposure = await asyncio.to_thread(
                    lambda: SitesLB(settings, log=_log).expose_cloud_run(cfg.website_name)
                )
                result.url = exposure["url"]
                result.resources = exposure["resources"]
                result.placement, result.lb_name = Placement.SITES.value, SitesLB.lb_name(settings)

        else:
            raise RuntimeError(f"Mode '{mode}' can no longer be deployed")

        if not result.success:
            raise RuntimeError(result.error or "Infrastructure provisioning failed")

        ctx.result_url = result.url
        ctx.bucket_name = result.storage_bucket
        ctx.upload_prefix = result.upload_prefix
        ctx.cdn_url_map, ctx.cdn_host, ctx.cdn_path = result.cdn_url_map, result.cdn_host, result.cdn_path
        if result.dns_nameservers:
            ctx.dns_nameservers = result.dns_nameservers

        lb_name = result.lb_name or {
            Placement.LEGACY_DEMO: settings.DEMO_URL_MAP_NAME,
            Placement.LEGACY_PROD: settings.PROD_URL_MAP_NAME,
        }.get(placement)
        is_prod = cfg.mode == DeploymentMode.PROD
        infra_update: dict = {
            "storage_bucket": result.storage_bucket or "",
            "backend_bucket": result.backend_bucket or "",
            "website_key": ctx.website_key,
            "infra_details": {
                "placement": placement.value,
                "lb_name": lb_name,
                "upload_prefix": result.upload_prefix,
                "ssl_cert_created": bool(is_prod and (placement == Placement.SITES or settings.PROD_AUTO_CREATE_SSL_CERT)),
                "dns_zone_created": bool(is_prod and settings.PROD_AUTO_CREATE_DNS_ZONE),
                "cdn_enabled": cfg.mode != DeploymentMode.CLOUDRUN,
                "run_app_url": run_app_url,
                "resources": result.resources,
            },
        }
        if result.dns_nameservers:
            infra_update["dns_nameservers"] = result.dns_nameservers
        await asyncio.to_thread(
            SessionLocal().collection("deployments").document(ctx.deployment_id).update,
            infra_update,
        )
        ctx.infra_resources = result.resources

    async def _step_upload(self, ctx: PipelineContext, log_cb: Callable) -> None:
        if ctx.config.mode == DeploymentMode.CLOUDRUN:
            log_cb(
                "Skipping upload — image already in Artifact Registry",
                level=LogLevel.INFO.value,
                step=PipelineStep.UPLOAD.value,
            )
            return

        from services.upload_service import UploadService

        service = UploadService(settings=self._settings, log_callback=log_cb)
        await asyncio.to_thread(
            service.upload,
            dist_path=ctx.dist_path,
            bucket_name=ctx.bucket_name,
            prefix=ctx.upload_prefix,
            main_html_file=ctx.main_html_file,
            dedicated_bucket=ctx.bucket_name != self._settings.sites_demo_bucket_name,
        )

        # Invalidate CDN cache so new content is served immediately
        await self._invalidate_cdn_cache(ctx, log_cb)

    async def _invalidate_cdn_cache(
        self, ctx: PipelineContext, log_cb: Callable,
    ) -> None:
        """Invalidate the CDN cache after uploading new content (best effort)."""
        if not ctx.cdn_url_map or not ctx.cdn_path:
            return
        from infra.gcp_helpers import get_credentials, wait_for_global_operation
        from googleapiclient.discovery import build

        body = {"path": ctx.cdn_path}
        if ctx.cdn_host:
            body["host"] = ctx.cdn_host
        log_cb(
            f"Invalidating CDN cache on {ctx.cdn_url_map} ({ctx.cdn_host or '*'}{ctx.cdn_path})",
            level=LogLevel.INFO.value,
            step=PipelineStep.UPLOAD.value,
        )
        try:
            credentials = get_credentials(self._settings.GOOGLE_APPLICATION_CREDENTIALS)
            compute = build("compute", "v1", credentials=credentials, cache_discovery=False)
            project_id = self._settings.PROJECT_ID

            def _invalidate():
                operation = compute.urlMaps().invalidateCache(
                    project=project_id, urlMap=ctx.cdn_url_map, body=body,
                ).execute()
                wait_for_global_operation(compute, project_id, operation["name"], timeout=120)

            await asyncio.to_thread(_invalidate)
            log_cb("CDN cache invalidated", level=LogLevel.INFO.value, step=PipelineStep.UPLOAD.value)
        except Exception as exc:
            # CDN invalidation failure should not block the deployment
            logger.warning("CDN cache invalidation failed: %s", exc)
            log_cb(
                f"CDN cache invalidation failed (non-fatal): {exc}",
                level=LogLevel.WARNING.value,
                step=PipelineStep.UPLOAD.value,
            )

    async def _step_notify(
        self,
        ctx: PipelineContext,
        log_cb: Callable,
        *,
        failed_step: Optional[PipelineStep] = None,
        failure_error: Optional[str] = None,
    ) -> None:
        from services.email_service import EmailService

        # Merge configured + per-deployment emails
        recipients = list(self._settings.notification_emails_list)
        if ctx.config.notification_emails:
            recipients.extend(ctx.config.notification_emails)
        recipients = list(set(r for r in recipients if r))

        # Filter out users who disabled deployment notifications
        if recipients:
            try:
                db = SessionLocal()
                filtered = []
                for email in recipients:
                    # Look up user by querying all users (small collection)
                    users_query = db.collection("users").where("email", "==", email).limit(1)
                    docs = list(users_query.stream())
                    if docs:
                        prefs = (docs[0].to_dict() or {}).get("notification_preferences", {})
                        if prefs and prefs.get("deployment_notifications") is False:
                            continue
                    filtered.append(email)
                recipients = filtered
            except Exception as exc:
                logger.warning("Failed to check notification preferences: %s", exc)

        if not recipients:
            log_cb(
                "No notification recipients configured — skipping email",
                level=LogLevel.WARNING.value,
                step=PipelineStep.NOTIFY.value,
            )
            return

        try:
            service = EmailService(settings=self._settings, log_callback=log_cb)
            if failed_step is not None:
                await service.send_notification(
                    website_name=ctx.config.website_name,
                    mode=ctx.config.mode.value,
                    success=False,
                    error_message=failure_error,
                    ai_summary=ctx.ai_summary,
                    recipients=recipients,
                )
            else:
                await service.send_notification(
                    website_name=ctx.config.website_name,
                    mode=ctx.config.mode.value,
                    success=True,
                    live_url=ctx.result_url,
                    ai_summary=ctx.ai_summary,
                    recipients=recipients,
                )
        except Exception as exc:
            # Notification failure must not mask the original pipeline outcome
            logger.exception("Failed to send notification email: %s", exc)
            log_cb(
                f"Email notification failed: {exc}",
                level=LogLevel.WARNING.value,
                step=PipelineStep.NOTIFY.value,
            )

    # ── Cleanup ───────────────────────────────────────────────────────

    def _cleanup(self, ctx: PipelineContext, log_cb: Callable) -> None:
        """Remove temporary extraction directories."""
        for path_attr in ("source_path", "dist_path"):
            path = getattr(ctx, path_attr, None)
            if not path:
                continue
            # Walk up to the temp root (created by ZipProcessingService)
            temp_base = str(self._settings.temp_path)
            if path.startswith(temp_base):
                # Find the first-level temp subdirectory
                relative = path[len(temp_base):].lstrip("/")
                top_level = relative.split("/")[0] if "/" in relative else relative
                cleanup_dir = f"{temp_base}/{top_level}"
                try:
                    shutil.rmtree(cleanup_dir, ignore_errors=True)
                    log_cb(
                        f"Cleaned up temp directory: {cleanup_dir}",
                        level=LogLevel.INFO.value,
                    )
                except Exception as exc:
                    logger.warning("Cleanup failed for %s: %s", cleanup_dir, exc)
                break  # Only clean once — source_path and dist_path share a parent
