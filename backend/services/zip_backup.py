"""
Backup and restore uploaded ZIPs to/from GCS.

Cloud Run containers have ephemeral local storage — files are lost on
restart.  This module ensures that uploaded ZIPs survive container
restarts so interrupted deployments can be automatically retried.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.cloud import storage

from config import Settings

logger = logging.getLogger("webdeploy.zip_backup")

_GCS_PREFIX = "uploads"


def _get_bucket(settings: Settings) -> storage.Bucket:
    """Return (and lazily create) the deploy-uploads GCS bucket."""
    client = storage.Client(project=settings.PROJECT_ID)
    bucket_name = settings.deploy_uploads_bucket_name
    bucket = client.bucket(bucket_name)

    if not bucket.exists():
        logger.info("Creating GCS bucket %s for ZIP backups", bucket_name)
        bucket = client.create_bucket(bucket_name, location=settings.BUCKET_LOCATION)

    return bucket


def backup_zip(settings: Settings, deployment_id: str, local_path: Path | str) -> None:
    """Upload a ZIP to GCS as a backup."""
    bucket = _get_bucket(settings)
    blob_name = f"{_GCS_PREFIX}/{deployment_id}.zip"
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local_path))
    logger.info("Backed up ZIP to gs://%s/%s", bucket.name, blob_name)


def restore_zip(settings: Settings, deployment_id: str, local_path: Path | str) -> bool:
    """Download a ZIP from GCS back to local disk.

    Returns True if the file was restored, False if not found in GCS.
    """
    bucket = _get_bucket(settings)
    blob_name = f"{_GCS_PREFIX}/{deployment_id}.zip"
    blob = bucket.blob(blob_name)

    if not blob.exists():
        logger.warning("No GCS backup found for deployment %s", deployment_id)
        return False

    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob.download_to_filename(str(local_path))
    logger.info("Restored ZIP from gs://%s/%s -> %s", bucket.name, blob_name, local_path)
    return True
