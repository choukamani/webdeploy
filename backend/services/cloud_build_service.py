"""
Cloud Build service — build Docker images via Google Cloud Build.

Creates a tarball of the source, uploads it to Cloud Storage, submits a build
request to Cloud Build, and polls until the build completes or fails.
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
import time
import uuid
from typing import Callable, Optional

from google.cloud import storage as gcs
from googleapiclient import discovery

from config import Settings
from infra.gcp_helpers import get_credentials

logger = logging.getLogger("webdeploy.cloud_build")

# Directories / patterns to exclude from the source tarball
_EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache"}
_EXCLUDE_PATTERNS = {".pyc", ".pyo", ".zip"}


class CloudBuildService:
    """Build Docker images via Google Cloud Build."""

    def __init__(self, settings: Settings, log_callback: Optional[Callable] = None) -> None:
        self._settings = settings
        self._log = log_callback or (lambda msg, **kw: None)
        self._credentials = get_credentials(settings.GOOGLE_APPLICATION_CREDENTIALS)
        self._project_id = settings.PROJECT_ID
        self._storage_client = gcs.Client(
            project=self._project_id,
            credentials=self._credentials,
        )
        self._cloudbuild = discovery.build(
            "cloudbuild", "v1", credentials=self._credentials, cache_discovery=False,
        )

    async def build_image(self, source_path: str, image_uri: str) -> str:
        """
        Build a Docker image from source via Cloud Build.

        Parameters
        ----------
        source_path : str
            Path to the project root containing a Dockerfile.
        image_uri : str
            Full image URI including tag, e.g.
            ``europe-west1-docker.pkg.dev/my-project/repo/image:tag``.

        Returns
        -------
        str
            The image URI on success.

        Raises
        ------
        RuntimeError
            If the build fails or times out.
        """
        import asyncio

        # 1. Create tarball
        self._log("Creating source tarball...", level="INFO", step="VERIFY")
        tarball_bytes = await asyncio.to_thread(self._create_tarball, source_path)
        self._log(
            f"Source tarball created ({len(tarball_bytes) / 1024 / 1024:.1f} MB)",
            level="INFO",
            step="VERIFY",
        )

        # 2. Upload tarball to Cloud Storage
        bucket_name = f"{self._project_id}_cloudbuild"
        blob_name = f"source/{uuid.uuid4()}.tar.gz"
        self._log(f"Uploading tarball to gs://{bucket_name}/{blob_name}", level="INFO", step="VERIFY")
        await asyncio.to_thread(self._upload_tarball, bucket_name, blob_name, tarball_bytes)

        # 3. Submit build
        self._log(f"Submitting Cloud Build for image: {image_uri}", level="INFO", step="VERIFY")
        build_id = await asyncio.to_thread(
            self._submit_build, bucket_name, blob_name, image_uri,
        )
        self._log(f"Cloud Build submitted — build ID: {build_id}", level="INFO", step="VERIFY")

        # 4. Poll until complete
        result = await asyncio.to_thread(self._poll_build, build_id)

        if result["status"] == "SUCCESS":
            self._log(f"Cloud Build succeeded — image: {image_uri}", level="INFO", step="VERIFY")
            return image_uri
        else:
            logs_url = result.get("logUrl", "N/A")
            # Try to fetch actual build log for better error messages
            build_log = await asyncio.to_thread(self._fetch_build_log, build_id)
            if build_log:
                self._log(
                    f"Cloud Build log (last 50 lines):\n{build_log}",
                    level="ERROR",
                    step="VERIFY",
                )
            raise RuntimeError(
                f"Cloud Build failed with status '{result['status']}'. "
                f"Logs: {logs_url}"
            )

    # ── Private helpers ────────────────────────────────────────────────

    def _create_tarball(self, source_path: str) -> bytes:
        """Create a gzipped tarball of the source directory."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for dirpath, dirnames, filenames in os.walk(source_path):
                # Skip excluded directories
                dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]

                for filename in filenames:
                    # Skip excluded file patterns
                    if any(filename.endswith(pat) for pat in _EXCLUDE_PATTERNS):
                        continue

                    full_path = os.path.join(dirpath, filename)
                    arcname = os.path.relpath(full_path, source_path)
                    # Normalize path separators for tar
                    arcname = arcname.replace("\\", "/")
                    tar.add(full_path, arcname=arcname)

        buf.seek(0)
        return buf.read()

    def _upload_tarball(self, bucket_name: str, blob_name: str, data: bytes) -> None:
        """Upload tarball bytes to Cloud Storage."""
        bucket = self._storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(data, content_type="application/gzip")
        logger.info("Uploaded tarball to gs://%s/%s", bucket_name, blob_name)

    def _submit_build(self, bucket_name: str, blob_name: str, image_uri: str) -> str:
        """Submit a Cloud Build request and return the build ID."""
        timeout_s = self._settings.CLOUD_BUILD_TIMEOUT_SECONDS

        build_body = {
            "source": {
                "storageSource": {
                    "bucket": bucket_name,
                    "object": blob_name,
                }
            },
            "steps": [
                {
                    "name": "gcr.io/cloud-builders/docker",
                    "args": ["build", "-t", image_uri, "."],
                }
            ],
            "images": [image_uri],
            "timeout": f"{timeout_s}s",
        }

        operation = (
            self._cloudbuild.projects()
            .builds()
            .create(projectId=self._project_id, body=build_body)
            .execute()
        )

        # The response contains metadata with the build ID
        build_id = operation.get("metadata", {}).get("build", {}).get("id")
        if not build_id:
            raise RuntimeError(f"Cloud Build did not return a build ID: {operation}")

        return build_id

    def _fetch_build_log(self, build_id: str) -> str:
        """Fetch the last 50 lines of the Cloud Build log from Cloud Storage."""
        try:
            bucket_name = f"{self._project_id}_cloudbuild"
            blob_name = f"log-{build_id}.txt"
            bucket = self._storage_client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            if not blob.exists():
                return ""
            content = blob.download_as_text(encoding="utf-8")
            lines = content.strip().splitlines()
            # Return last 50 lines to keep it manageable
            return "\n".join(lines[-50:])
        except Exception as exc:
            logger.warning("Could not fetch build log for %s: %s", build_id, exc)
            return ""

    def _poll_build(self, build_id: str) -> dict:
        """Poll Cloud Build until the build completes or fails."""
        timeout = self._settings.CLOUD_BUILD_TIMEOUT_SECONDS + 60  # extra buffer
        deadline = time.monotonic() + timeout
        poll_interval = 10.0

        while True:
            result = (
                self._cloudbuild.projects()
                .builds()
                .get(projectId=self._project_id, id=build_id)
                .execute()
            )

            status = result.get("status", "UNKNOWN")
            self._log(f"Cloud Build status: {status}", level="INFO", step="VERIFY")

            if status in ("SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED"):
                return result

            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Cloud Build {build_id} timed out after {timeout}s"
                )

            time.sleep(poll_interval)
