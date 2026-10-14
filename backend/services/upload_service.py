"""
Upload service — upload validated dist/ to a Google Cloud Storage bucket.

Handles MIME type detection, cache-control headers, parallel uploads via
concurrent.futures, and progress reporting through a log callback.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from google.cloud import storage

from config import Settings

logger = logging.getLogger("webdeploy.upload_service")

# ── MIME type map — overrides / additions beyond Python's built-in map ────
_CONTENT_TYPE_MAP: dict[str, str] = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".otf": "font/otf",
    ".xml": "application/xml",
    ".txt": "text/plain",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
}

# HTML files get no-cache so users always see the latest deployment.
# All other assets (JS, CSS, images, fonts) get long cache with public access.
_CACHE_CONTROL_HTML = "no-cache, no-store, must-revalidate"
_CACHE_CONTROL_ASSETS = "public, max-age=3600"

# Max parallel uploads
_MAX_WORKERS = 10


class UploadService:
    """Upload a dist/ directory to Google Cloud Storage."""

    def __init__(
        self,
        log_callback: Optional[Callable] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._log = log_callback or (lambda msg, **kw: None)
        self._settings = settings or Settings()
        self._client: Optional[storage.Client] = None

    # ── Public API ────────────────────────────────────────────────────

    def upload(
        self,
        dist_path: str,
        bucket_name: str,
        prefix: str = "",
        main_html_file: str = "index.html",
        dedicated_bucket: bool = True,
    ) -> int:
        """
        Publish *dist_path* as the website's current version.

        Parameters
        ----------
        dist_path : str
            Absolute path to the built ``dist/`` directory.
        bucket_name : str
            GCS bucket name.
        prefix : str
            Object prefix the site lives under ("{site}/" for path-routed
            demo sites, "" for a dedicated bucket).
        main_html_file : str
            Entry page. Buckets serve ``index.html`` for directory URLs, so a
            differently-named entry page is also published as ``index.html``.
        dedicated_bucket : bool
            The bucket belongs to this website alone (False for the shared
            demo bucket), so its website config is aligned to ``index.html``
            on every deploy. Older buckets were configured once at creation
            with whatever the first version's entry page was called.

        After every file uploads, objects under *prefix* that the new version
        no longer contains are deleted: a redeploy replaces the site instead
        of layering on top of it (old hashed bundles and removed pages used to
        stay reachable forever). Pruning runs only after a fully successful
        upload, so a failed deploy never leaves the site half-emptied.

        Returns the number of files uploaded.

        Raises
        ------
        RuntimeError
            If any file fails to upload (after retries).
        """
        if prefix and (not prefix.endswith("/") or prefix.startswith("/") or ".." in prefix):
            raise ValueError(f"Invalid upload prefix {prefix!r}")
        if not dedicated_bucket and not prefix:
            # Pruning a shared bucket at its root would delete every other site.
            raise ValueError("A shared bucket needs a per-site upload prefix")
        self._log(f"Starting upload to gs://{bucket_name}/{prefix}", level="INFO", step="UPLOAD")

        file_list = self._collect_files(dist_path)
        total = len(file_list)
        if total == 0:
            raise RuntimeError(f"No files found in dist directory: {dist_path}")

        names = {rel for rel, _ in file_list}
        if "index.html" not in names and main_html_file in names:
            source = dict(file_list)[main_html_file]
            file_list.append(("index.html", source))
            self._log(
                f"Entry page is {main_html_file} — also publishing it as index.html",
                level="INFO", step="UPLOAD",
            )
        total = len(file_list)
        self._log(f"Found {total} file(s) to upload", level="INFO", step="UPLOAD")

        client = self._get_client()
        bucket = client.bucket(bucket_name)

        uploaded = 0
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    self._upload_file, bucket=bucket, abs_path=abs_path,
                    object_name=f"{prefix}{rel_path}",
                ): rel_path
                for rel_path, abs_path in file_list
            }
            for future in as_completed(futures):
                rel_path = futures[future]
                try:
                    future.result()
                    uploaded += 1
                    if uploaded % 10 == 0 or uploaded == total:
                        self._log(f"Uploaded {uploaded}/{total} files", level="INFO", step="UPLOAD")
                except Exception as exc:
                    failures.append(rel_path)
                    self._log(f"Failed to upload {rel_path}: {exc}", level="ERROR", step="UPLOAD")

        if failures:
            # A site with missing files is broken, not "mostly deployed".
            raise RuntimeError(
                f"{len(failures)} of {total} files failed to upload "
                f"(e.g. {', '.join(sorted(failures)[:3])}). Check GCS permissions and retry."
            )

        if dedicated_bucket:
            # Before pruning: the old entry page may be what the config points at.
            from infra.resources import set_website_config
            set_website_config(client.get_bucket(bucket_name), "index.html", "index.html")

        pruned = self._prune(client, bucket_name, prefix, {f"{prefix}{rel}" for rel, _ in file_list})
        self._log(
            f"Upload complete: {uploaded} files to gs://{bucket_name}/{prefix}"
            + (f", {pruned} stale file(s) from the previous version removed" if pruned else ""),
            level="INFO",
            step="UPLOAD",
        )
        return uploaded

    def _prune(self, client: storage.Client, bucket_name: str, prefix: str, keep: set[str]) -> int:
        stale = [
            blob for blob in client.list_blobs(bucket_name, prefix=prefix or None)
            if blob.name not in keep
        ]
        for blob in stale:
            try:
                blob.delete()
            except Exception as exc:
                self._log(f"Could not remove stale {blob.name}: {exc}", level="WARNING", step="UPLOAD")
        return len(stale)

    # ── Private helpers ───────────────────────────────────────────────

    def _get_client(self) -> storage.Client:
        """Lazy-init the GCS client."""
        if self._client is None:
            creds_path = self._settings.GOOGLE_APPLICATION_CREDENTIALS
            if creds_path and os.path.isfile(creds_path):
                self._client = storage.Client.from_service_account_json(creds_path)
                self._log("GCS client initialized from service account JSON", level="INFO", step="UPLOAD")
            else:
                # Fall back to Application Default Credentials
                self._client = storage.Client(project=self._settings.PROJECT_ID)
                self._log("GCS client initialized with default credentials", level="INFO", step="UPLOAD")
        return self._client

    def _collect_files(self, dist_path: str) -> list[tuple[str, str]]:
        """
        Walk the dist/ tree and return a list of (relative_path, absolute_path)
        tuples for every file.
        """
        files: list[tuple[str, str]] = []
        for dirpath, _, filenames in os.walk(dist_path):
            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, dist_path)
                # Normalise to forward slashes for GCS object names
                rel_path = rel_path.replace(os.sep, "/")
                files.append((rel_path, abs_path))
        return files

    def _upload_file(
        self,
        bucket: storage.Bucket,
        abs_path: str,
        object_name: str,
        max_retries: int = 3,
    ) -> None:
        """Upload a single file to GCS with correct Content-Type and Cache-Control."""
        content_type = self._detect_content_type(abs_path)
        cache_control = self._get_cache_control(abs_path)

        import time as _time

        for attempt in range(1, max_retries + 1):
            try:
                blob = bucket.blob(object_name)
                blob.content_type = content_type
                blob.cache_control = cache_control
                blob.upload_from_filename(abs_path)
                return
            except Exception as exc:
                if attempt == max_retries:
                    raise
                logger.warning(
                    "Upload attempt %d/%d failed for %s: %s — retrying",
                    attempt, max_retries, object_name, exc,
                )
                _time.sleep(2 ** attempt)

    @staticmethod
    def _detect_content_type(file_path: str) -> str:
        """Determine the MIME type for a file based on its extension."""
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        # Check our explicit map first
        if ext in _CONTENT_TYPE_MAP:
            return _CONTENT_TYPE_MAP[ext]

        # Fall back to Python's mimetypes module
        guessed, _ = mimetypes.guess_type(file_path)
        return guessed or "application/octet-stream"

    @staticmethod
    def _get_cache_control(file_path: str) -> str:
        """
        Return the appropriate Cache-Control header.
        HTML files get no-cache; everything else gets public caching.
        """
        _, ext = os.path.splitext(file_path)
        if ext.lower() in (".html", ".htm"):
            return _CACHE_CONTROL_HTML
        return _CACHE_CONTROL_ASSETS
