"""
CloudRunDeployer — deploy a container image to Google Cloud Run.

Follows the same pattern as DemoDeployer and ProdDeployer: idempotent
create-or-update with an async ``deploy()`` entry point.

Uses the Cloud Run Admin API v2 to manage services and the IAM API to
allow unauthenticated access (``allUsers`` with ``roles/run.invoker``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import google.auth.transport.requests
from googleapiclient import discovery

from config import Settings
from models.deployment import DeploymentResult
from infra.gcp_helpers import get_credentials
from infra.layout import app_names

logger = logging.getLogger(__name__)


class CloudRunDeployer:
    """Deploy a container image to Cloud Run.

    Args:
        config: Application-wide settings (see ``config.Settings``).
        log_callback: An ``async`` callable ``(str) -> None`` used to stream
            progress messages back to the caller.
    """

    def __init__(self, config: Settings, log_callback: Callable) -> None:
        self._config = config
        self._log = log_callback
        self._credentials = get_credentials(config.GOOGLE_APPLICATION_CREDENTIALS)
        self._project_id = config.PROJECT_ID
        self._region = config.CLOUDRUN_REGION

        # Cloud Run Admin API v2
        self._run_v2 = discovery.build(
            "run", "v2", credentials=self._credentials, cache_discovery=False,
        )

    # ── helpers ─────────────────────────────────────────────────────────

    async def _emit(self, message: str) -> None:
        try:
            await self._log(message)
        except Exception:
            logger.warning("log_callback failed for message: %s", message)

    # ── public entry point ──────────────────────────────────────────────

    async def deploy(self, website_name: str, image_uri: str) -> DeploymentResult:
        """Deploy *image_uri* to Cloud Run as service *website_name*.

        The operation is idempotent: if the service already exists it is updated
        (PATCH); otherwise it is created.

        Returns a ``DeploymentResult`` with the public ``.run.app`` URL.
        """
        service_id = app_names(website_name, self._config).service
        parent = f"projects/{self._project_id}/locations/{self._region}"
        service_name = f"{parent}/services/{service_id}"

        await self._emit(f"[INFRA] Starting Cloud Run deployment for '{website_name}' (service: {service_id})")

        try:
            # Build the service spec
            service_body = self._build_service_spec(service_name, image_uri)

            # Check if service already exists
            exists = await asyncio.to_thread(self._service_exists, service_name)

            if exists:
                await self._emit(f"[INFRA] Service '{service_id}' exists — updating")
                operation = await asyncio.to_thread(
                    self._update_service, service_name, service_body,
                )
            else:
                await self._emit(f"[INFRA] Creating new service '{service_id}'")
                operation = await asyncio.to_thread(
                    self._create_service, parent, service_id, service_body,
                )

            # Wait for the operation to complete
            await self._emit("[INFRA] Waiting for Cloud Run deployment to complete...")
            await asyncio.to_thread(self._wait_for_operation, operation["name"])

            # Set IAM policy for unauthenticated access
            await self._emit("[INFRA] Setting IAM policy for public access")
            await asyncio.to_thread(self._set_public_access, service_name)

            # Fetch the service to get the URL
            service = await asyncio.to_thread(self._get_service, service_name)
            url = service.get("uri", "")

            await self._emit(f"[INFRA] Cloud Run deployment complete: {url}")

            return DeploymentResult(
                mode="cloudrun",
                website_name=website_name,
                success=True,
                url=url,
                cloudrun_service=service_id,
                docker_image=image_uri,
            )

        except Exception as exc:
            error_msg = f"Cloud Run deployment failed: {exc}"
            logger.exception(error_msg)
            await self._emit(f"[INFRA] ERROR: {error_msg}")
            return DeploymentResult(
                mode="cloudrun",
                website_name=website_name,
                success=False,
                error=error_msg,
                cloudrun_service=service_id,
                docker_image=image_uri,
            )

    # ── Service spec ───────────────────────────────────────────────────

    def _build_service_spec(self, service_name: str, image_uri: str) -> dict[str, Any]:
        """Build the Cloud Run v2 service resource body."""
        return {
            "template": {
                "containers": [
                    {
                        "image": image_uri,
                        "ports": [{"containerPort": 8080}],
                        "resources": {
                            "limits": {
                                "memory": self._config.CLOUDRUN_MEMORY,
                                "cpu": self._config.CLOUDRUN_CPU,
                            },
                        },
                    }
                ],
                "scaling": {
                    "maxInstanceCount": self._config.CLOUDRUN_MAX_INSTANCES,
                    "minInstanceCount": self._config.CLOUDRUN_MIN_INSTANCES,
                },
            },
        }

    # ── CRUD operations ────────────────────────────────────────────────

    def _service_exists(self, service_name: str) -> bool:
        """Check whether a Cloud Run service exists."""
        try:
            self._run_v2.projects().locations().services().get(
                name=service_name,
            ).execute()
            return True
        except Exception:
            return False

    def _get_service(self, service_name: str) -> dict:
        """Get the Cloud Run service resource."""
        return (
            self._run_v2.projects().locations().services()
            .get(name=service_name)
            .execute()
        )

    def _create_service(
        self, parent: str, service_id: str, body: dict,
    ) -> dict:
        """Create a new Cloud Run service."""
        return (
            self._run_v2.projects().locations().services()
            .create(parent=parent, serviceId=service_id, body=body)
            .execute()
        )

    def _update_service(self, service_name: str, body: dict) -> dict:
        """Update an existing Cloud Run service."""
        return (
            self._run_v2.projects().locations().services()
            .patch(name=service_name, body=body)
            .execute()
        )

    def _wait_for_operation(self, operation_name: str, timeout: int = 300) -> dict:
        """Poll a Cloud Run long-running operation until completion."""
        deadline = time.monotonic() + timeout
        poll_interval = 5.0

        while True:
            result = (
                self._run_v2.projects().locations().operations()
                .get(name=operation_name)
                .execute()
            )

            if result.get("done"):
                if "error" in result:
                    error = result["error"]
                    raise RuntimeError(
                        f"Cloud Run operation failed: {error.get('message', error)}"
                    )
                return result

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Cloud Run operation {operation_name} timed out after {timeout}s"
                )

            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.3, 15.0)

    def _set_public_access(self, service_name: str) -> None:
        """Allow unauthenticated access by granting allUsers the invoker role."""
        policy = {
            "bindings": [
                {
                    "role": "roles/run.invoker",
                    "members": ["allUsers"],
                }
            ],
        }

        self._run_v2.projects().locations().services().setIamPolicy(
            resource=service_name,
            body={"policy": policy},
        ).execute()

        logger.info("Set public access (allUsers -> run.invoker) on %s", service_name)
