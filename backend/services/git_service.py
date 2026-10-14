"""
Git service — download a repo snapshot (zip) from GitHub or GitLab via API.

Uses HTTP archive endpoints so no ``git`` binary is required at runtime.
Supports private repos via Personal Access Token (passed in ``Authorization``).

GitHub:  GET /repos/{owner}/{repo}/zipball/{ref}
GitLab:  GET /projects/{url_encoded_path}/repository/archive.zip?sha={ref}
"""

from __future__ import annotations

import io
import logging
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("webdeploy.git_service")


class GitDownloadError(RuntimeError):
    """Raised when downloading a repo snapshot fails."""


def _parse_github_url(repo_url: str) -> tuple[str, str]:
    """Extract owner/repo from a GitHub URL.

    Accepts formats:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git
    """
    # SSH form
    ssh_match = re.match(r"git@github\.com:([^/]+)/([^/.]+)", repo_url)
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)

    # HTTPS form
    parsed = urlparse(repo_url.rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise GitDownloadError(f"Cannot parse GitHub repo URL: {repo_url}")

    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    return owner, repo


def _parse_gitlab_path(repo_url: str) -> str:
    """Extract the project path (owner/repo or group/subgroup/repo) from a GitLab URL."""
    ssh_match = re.match(r"git@gitlab\.com:(.+?)(?:\.git)?$", repo_url)
    if ssh_match:
        return ssh_match.group(1)

    parsed = urlparse(repo_url.rstrip("/"))
    path = parsed.path.strip("/").removesuffix(".git")
    if not path:
        raise GitDownloadError(f"Cannot parse GitLab repo URL: {repo_url}")
    return path


def download_repo_as_zip(
    *,
    provider: str,
    repo_url: str,
    ref: str,
    access_token: str,
    dest_zip_path: Path,
    timeout: int = 120,
) -> Path:
    """Download a zip snapshot of *repo_url* at *ref* and save to *dest_zip_path*.

    The downloaded archive from GitHub/GitLab has a single top-level folder
    (e.g. ``owner-repo-abc1234/``).  This function **strips that prefix** so
    the resulting zip has files at the root — matching the format the
    deployment pipeline expects from user uploads.

    Args:
        provider: ``"github"`` or ``"gitlab"``.
        repo_url: HTTPS or SSH repo URL.
        ref: Branch name, tag, or commit SHA.
        access_token: Personal Access Token with ``repo`` / ``read_repository`` scope.
        dest_zip_path: Where to write the re-packaged zip.
        timeout: HTTP timeout in seconds.

    Returns:
        Path to the written zip file.

    Raises:
        GitDownloadError: On HTTP failure or parsing error.
    """
    provider = (provider or "").lower()

    if provider == "github":
        owner, repo = _parse_github_url(repo_url)
        url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{ref}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "webdeploy",
        }
    elif provider == "gitlab":
        project_path = _parse_gitlab_path(repo_url)
        encoded_path = urllib.parse.quote(project_path, safe="")
        url = (
            f"https://gitlab.com/api/v4/projects/{encoded_path}"
            f"/repository/archive.zip?sha={urllib.parse.quote(ref, safe='')}"
        )
        headers = {
            "PRIVATE-TOKEN": access_token,
            "User-Agent": "webdeploy",
        }
    else:
        raise GitDownloadError(f"Unsupported git provider: {provider!r}")

    logger.info("Downloading %s repo %s @ %s", provider, repo_url, ref)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        raise GitDownloadError(
            f"Download failed ({exc.response.status_code}): {body}"
        ) from exc
    except httpx.HTTPError as exc:
        raise GitDownloadError(f"HTTP error downloading repo: {exc}") from exc

    # ─── Strip the top-level folder from the archive ───────────────────
    try:
        src_buf = io.BytesIO(resp.content)
        with zipfile.ZipFile(src_buf, "r") as src_zip:
            names = src_zip.namelist()
            if not names:
                raise GitDownloadError("Downloaded archive is empty")

            # Detect the common prefix (e.g. "owner-repo-abc1234/")
            first = names[0].replace("\\", "/")
            prefix = first.split("/", 1)[0] + "/"

            dest_zip_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as dest_zip:
                for name in names:
                    normalised = name.replace("\\", "/")
                    # Skip directory entries and the prefix root itself
                    if normalised.endswith("/"):
                        continue
                    # Strip the prefix
                    if normalised.startswith(prefix):
                        arcname = normalised[len(prefix):]
                    else:
                        arcname = normalised
                    if not arcname:
                        continue
                    data = src_zip.read(name)
                    dest_zip.writestr(arcname, data)
    except zipfile.BadZipFile as exc:
        raise GitDownloadError(f"Downloaded file is not a valid zip: {exc}") from exc

    logger.info("Wrote repo snapshot to %s (%d bytes)", dest_zip_path, dest_zip_path.stat().st_size)
    return dest_zip_path
