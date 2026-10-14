"""
DemoDeployer — legacy demo placement on ``legacy-demo-lb``.

Only used to *redeploy* demo sites that already live on legacy-demo-lb, so their
``https://example.com/{website_name}/`` URLs keep working. New demo
sites go to the dedicated sites LB (``infra.sites_lb``): legacy-demo-lb's URL map is
full. Deletion of these sites is handled by ``infra.teardown``.

All operations are **idempotent**: resources are checked for existence before
creation, and path rules are only appended if they do not already exist.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from google.cloud import storage as gcs
from googleapiclient import discovery, errors as api_errors

from config import Settings
from models.deployment import DeploymentResult
from infra.gcp_helpers import (
    get_backend_bucket_name,
    get_bucket_name,
    get_credentials,
    mutate_url_map,
    safe_name,
    wait_for_global_operation,
)

logger = logging.getLogger(__name__)


class DemoDeployer:
    """Deploy a website to the shared demo load-balancer infrastructure.

    Args:
        config: Application-wide settings (see ``config.Settings``).
        log_callback: An ``async`` callable ``(str) -> None`` used to stream
            progress messages back to the caller (e.g. WebSocket, DB log).
    """

    def __init__(self, config: Settings, log_callback: Callable) -> None:
        self._config = config
        self._log = log_callback

        # Authenticate
        self._credentials = get_credentials(config.GOOGLE_APPLICATION_CREDENTIALS)
        self._project_id = config.PROJECT_ID

        # API clients
        self._storage_client = gcs.Client(
            project=self._project_id,
            credentials=self._credentials,
        )
        self._compute = discovery.build(
            "compute", "v1", credentials=self._credentials, cache_discovery=False,
        )

    # ─── helpers ───────────────────────────────────────────────────────

    async def _emit(self, message: str) -> None:
        """Send a progress message through the log callback."""
        try:
            await self._log(message)
        except Exception:
            logger.warning("log_callback failed for message: %s", message)

    def _run_sync(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking function in the default executor."""
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, func, *args)

    # ─── public entry point ────────────────────────────────────────────

    async def deploy(self, website_name: str, main_html_file: str = "index.html") -> DeploymentResult:
        """Provision demo infrastructure for *website_name*.

        Returns a ``DeploymentResult`` with the public URL on success,
        or an error description on failure.
        """
        sname = safe_name(website_name)
        bucket_name = get_bucket_name(website_name, "demo")
        backend_bucket_name = get_backend_bucket_name(website_name, "demo")

        await self._emit(f"[INFRA] Starting demo deployment for '{website_name}' (safe: {sname})")

        try:
            # Step 1 — Storage bucket
            await self._ensure_storage_bucket(bucket_name, main_html_file)

            # Step 2 — Backend bucket (CDN)
            await self._ensure_backend_bucket(backend_bucket_name, bucket_name)

            # Step 3 — Path rule on shared URL map
            await self._ensure_url_map_path_rule(
                website_name, backend_bucket_name,
            )

            url = f"https://{self._config.DEMO_DOMAIN}/{website_name}/"
            await self._emit(f"[INFRA] Demo deployment complete: {url}")

            return DeploymentResult(
                mode="demo",
                website_name=website_name,
                success=True,
                url=url,
                storage_bucket=bucket_name,
                backend_bucket=backend_bucket_name,
                url_map_updated=True,
            )

        except Exception as exc:
            error_msg = f"Demo deployment failed: {exc}"
            logger.exception(error_msg)
            await self._emit(f"[INFRA] ERROR: {error_msg}")
            return DeploymentResult(
                mode="demo",
                website_name=website_name,
                success=False,
                error=error_msg,
                storage_bucket=bucket_name,
                backend_bucket=backend_bucket_name,
            )

    # =================================================================
    #  Step 1 — Storage Bucket
    # =================================================================

    async def _ensure_storage_bucket(self, bucket_name: str, main_html_file: str = "index.html") -> None:
        """Create the Cloud Storage bucket if it does not already exist."""
        await self._emit(f"[INFRA] Checking storage bucket: {bucket_name}")

        def _create() -> None:
            try:
                bucket = self._storage_client.get_bucket(bucket_name)
                logger.info("Bucket %s already exists — skipping creation.", bucket_name)
                return
            except Exception:
                pass  # bucket does not exist; create it below

            logger.info("Creating bucket %s ...", bucket_name)
            bucket = self._storage_client.bucket(bucket_name)
            bucket.iam_configuration.uniform_bucket_level_access_enabled = True
            bucket.versioning_enabled = False
            bucket.cors = [
                {
                    "origin": [f"https://{self._config.DEMO_DOMAIN}"],
                    "method": ["GET", "HEAD", "OPTIONS"],
                    "responseHeader": [
                        "Content-Type",
                        "Access-Control-Allow-Origin",
                        "x-goog-meta-*",
                    ],
                    "maxAgeSeconds": self._config.BUCKET_CORS_MAX_AGE,
                }
            ]
            bucket.create(location=self._config.BUCKET_LOCATION)

            # Website configuration
            bucket.configure_website(
                main_page_suffix=main_html_file,
                not_found_page=main_html_file,
            )
            bucket.patch()

            # Public read access
            policy = bucket.get_iam_policy(requested_policy_version=3)
            policy.bindings.append(
                {
                    "role": "roles/storage.objectViewer",
                    "members": {"allUsers"},
                }
            )
            bucket.set_iam_policy(policy)

            logger.info("Bucket %s created and configured.", bucket_name)

        await self._run_sync(_create)
        await self._emit(f"[INFRA] Storage bucket ready: {bucket_name}")

    # =================================================================
    #  Step 2 — Backend Bucket (CDN)
    # =================================================================

    async def _ensure_backend_bucket(
        self, backend_bucket_name: str, storage_bucket_name: str,
    ) -> None:
        """Create a Compute Engine backend bucket linked to the storage bucket."""
        await self._emit(f"[INFRA] Checking backend bucket: {backend_bucket_name}")

        def _create() -> None:
            # Check existence
            try:
                self._compute.backendBuckets().get(
                    project=self._project_id, backendBucket=backend_bucket_name,
                ).execute()
                logger.info("Backend bucket %s already exists — skipping.", backend_bucket_name)
                return
            except api_errors.HttpError as err:
                if err.resp.status != 404:
                    raise

            body: dict[str, Any] = {
                "name": backend_bucket_name,
                "bucketName": storage_bucket_name,
                "enableCdn": True,
                "cdnPolicy": {
                    "cacheMode": "CACHE_ALL_STATIC",
                    "defaultTtl": self._config.CDN_DEFAULT_TTL,
                    "maxTtl": self._config.CDN_MAX_TTL,
                    "clientTtl": self._config.CDN_CLIENT_TTL,
                    "negativeCaching": self._config.CDN_NEGATIVE_CACHING,
                    "negativeCachingPolicy": [
                        {"code": 404, "ttl": self._config.CDN_NEGATIVE_CACHING_TTL},
                        {"code": 410, "ttl": self._config.CDN_NEGATIVE_CACHING_TTL},
                    ],
                },
                "compressionMode": "AUTOMATIC",
                "customResponseHeaders": [
                    "X-Content-Type-Options:nosniff",
                ],
            }

            operation = (
                self._compute.backendBuckets()
                .insert(project=self._project_id, body=body)
                .execute()
            )
            wait_for_global_operation(
                self._compute, self._project_id, operation["name"],
            )
            logger.info("Backend bucket %s created.", backend_bucket_name)

        await self._run_sync(_create)
        await self._emit(f"[INFRA] Backend bucket ready: {backend_bucket_name}")

    # =================================================================
    #  Step 3 — URL Map Path Rule
    # =================================================================

    async def _ensure_url_map_path_rule(
        self, website_name: str, backend_bucket_name: str,
    ) -> None:
        """Add path rules for the website to the shared demo URL map.

        Appends ``/{website_name}`` and ``/{website_name}/*`` pointing to the
        backend bucket.  Existing rules are preserved; if the paths are already
        present the operation is a no-op.
        """
        url_map_name = self._config.DEMO_URL_MAP_NAME
        await self._emit(
            f"[INFRA] Updating URL map '{url_map_name}' with path rule for /{website_name}"
        )

        def _update() -> None:
            # Resolve the full self-link for the backend bucket once; it
            # doesn't change across retries.
            bb_resource = (
                self._compute.backendBuckets()
                .get(project=self._project_id, backendBucket=backend_bucket_name)
                .execute()
            )
            bb_self_link = bb_resource["selfLink"]
            desired_paths = [f"/{website_name}", f"/{website_name}/*"]

            def _mutate(url_map: dict) -> bool:
                # Find the path matcher that handles the demo domain.
                host_rules: list[dict] = url_map.get("hostRules", [])
                target_matcher_name: str | None = None
                for hr in host_rules:
                    if self._config.DEMO_DOMAIN in hr.get("hosts", []):
                        target_matcher_name = hr.get("pathMatcher")
                        break

                if target_matcher_name is None:
                    raise RuntimeError(
                        f"No host rule for '{self._config.DEMO_DOMAIN}' found in "
                        f"URL map '{url_map_name}'."
                    )

                target_matcher: dict | None = None
                for pm in url_map.get("pathMatchers", []):
                    if pm.get("name") == target_matcher_name:
                        target_matcher = pm
                        break

                if target_matcher is None:
                    raise RuntimeError(
                        f"Path matcher '{target_matcher_name}' referenced by host "
                        f"rule but not found in URL map '{url_map_name}'."
                    )

                # Check for existing path rules that already cover our paths
                existing_rules: list[dict] = target_matcher.get("pathRules", [])
                existing_paths: set[str] = {
                    p for rule in existing_rules for p in rule.get("paths", [])
                }

                if all(p in existing_paths for p in desired_paths):
                    logger.info(
                        "Path rules for %s already exist in URL map — skipping.",
                        desired_paths,
                    )
                    return False

                # Remove any partial matches and re-add the complete rule.
                cleaned_rules = [
                    rule for rule in existing_rules
                    if not any(p in rule.get("paths", []) for p in desired_paths)
                ]
                cleaned_rules.append({"paths": desired_paths, "service": bb_self_link})
                target_matcher["pathRules"] = cleaned_rules
                return True

            mutate_url_map(self._compute, self._project_id, url_map_name, _mutate)
            logger.info(
                "URL map '%s' updated with paths %s -> %s",
                url_map_name, desired_paths, backend_bucket_name,
            )

        await self._run_sync(_update)
        await self._emit(f"[INFRA] URL map updated for /{website_name}")
