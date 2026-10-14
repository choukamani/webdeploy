"""Preview API — extract a ZIP and serve it temporarily for sandbox preview."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse

from config import Settings, get_settings
from api.dependencies import require_approved

logger = logging.getLogger("webdeploy.api.preview")

router = APIRouter(prefix="/api/preview", tags=["preview"])

# In-memory store of active previews: preview_id -> { path, created_at, cleanup_task }
_active_previews: dict[str, dict] = {}

# Preview auto-cleanup after 15 minutes
_PREVIEW_TTL_SECONDS = 900


async def _cleanup_preview(preview_id: str, delay: float) -> None:
    """Remove preview files after TTL."""
    await asyncio.sleep(delay)
    info = _active_previews.pop(preview_id, None)
    if info and info.get("path"):
        try:
            shutil.rmtree(info["path"], ignore_errors=True)
            logger.info("Preview %s cleaned up", preview_id)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  POST /api/preview — upload ZIP and create temporary preview
# ═══════════════════════════════════════════════════════════════════════

@router.post("")
async def create_preview(
    zip_file: Optional[UploadFile] = File(None),
    files: list[UploadFile] = File(default=[]),
    user=Depends(require_approved),
    settings: Settings = Depends(get_settings),
):
    """Extract uploaded files to a temp directory and return a preview URL."""
    has_zip = zip_file is not None and zip_file.filename
    has_files = len(files) > 0 and any(f.filename for f in files)

    if not has_zip and not has_files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No file uploaded.",
        )

    preview_id = str(uuid.uuid4())
    preview_dir = Path(tempfile.mkdtemp(prefix=f"preview-{preview_id[:8]}-"))

    try:
        if has_zip:
            # Save and extract ZIP
            zip_path = preview_dir / "upload.zip"
            async with aiofiles.open(str(zip_path), "wb") as f:
                while chunk := await zip_file.read(1024 * 1024):
                    await f.write(chunk)

            extract_dir = preview_dir / "site"
            extract_dir.mkdir()
            with zipfile.ZipFile(str(zip_path), "r") as zf:
                zf.extractall(str(extract_dir))

            # Find the actual root (may be nested in a single folder)
            entries = list(extract_dir.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                site_root = entries[0]
            else:
                site_root = extract_dir

            # Look for dist/build output first
            for candidate in ("dist", "build", "public", "out"):
                candidate_path = site_root / candidate
                if candidate_path.is_dir() and (candidate_path / "index.html").exists():
                    site_root = candidate_path
                    break

            zip_path.unlink(missing_ok=True)
        else:
            # Raw files
            site_root = preview_dir / "site"
            site_root.mkdir()

            names = [f.filename for f in files if f.filename]
            common_prefix = ""
            if len(names) > 1:
                parts_list = [n.replace("\\", "/").split("/") for n in names]
                if all(len(p) > 1 for p in parts_list):
                    first_segment = parts_list[0][0]
                    if all(p[0] == first_segment for p in parts_list):
                        common_prefix = first_segment + "/"

            for upload in files:
                content = await upload.read()
                rel_path = upload.filename.replace("\\", "/")
                if common_prefix and rel_path.startswith(common_prefix):
                    rel_path = rel_path[len(common_prefix):]
                if not rel_path:
                    continue
                dest = site_root / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(str(dest), "wb") as f:
                    await f.write(content)

    except Exception as exc:
        shutil.rmtree(str(preview_dir), ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process files for preview: {exc}",
        ) from exc

    # Register preview
    cleanup_task = asyncio.create_task(
        _cleanup_preview(preview_id, _PREVIEW_TTL_SECONDS),
        name=f"preview-cleanup-{preview_id[:8]}",
    )
    _active_previews[preview_id] = {
        "path": str(site_root),
        "cleanup_task": cleanup_task,
    }

    logger.info("Preview %s created at %s (TTL=%ds)", preview_id, site_root, _PREVIEW_TTL_SECONDS)
    return {
        "preview_id": preview_id,
        "url": f"/api/preview/{preview_id}/",
        "ttl_seconds": _PREVIEW_TTL_SECONDS,
    }


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/preview/{preview_id}/{path} — serve preview files
# ═══════════════════════════════════════════════════════════════════════

@router.get("/{preview_id}/{file_path:path}")
async def serve_preview_file(preview_id: str, file_path: str = ""):
    """Serve a file from an active preview."""
    info = _active_previews.get(preview_id)
    if not info:
        raise HTTPException(status_code=404, detail="Preview not found or expired.")

    site_root = Path(info["path"])
    requested = site_root / (file_path or "index.html")

    # If path is a directory, serve index.html
    if requested.is_dir():
        requested = requested / "index.html"

    if not requested.exists():
        # SPA fallback
        fallback = site_root / "index.html"
        if fallback.exists():
            return FileResponse(str(fallback), media_type="text/html")
        raise HTTPException(status_code=404, detail="File not found.")

    # Resolve path to prevent directory traversal
    try:
        requested.resolve().relative_to(site_root.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.")

    return FileResponse(str(requested))


# ═══════════════════════════════════════════════════════════════════════
#  DELETE /api/preview/{preview_id} — remove preview early
# ═══════════════════════════════════════════════════════════════════════

@router.delete("/{preview_id}")
async def delete_preview(preview_id: str, user=Depends(require_approved)):
    """Manually delete a preview before TTL expires."""
    info = _active_previews.pop(preview_id, None)
    if not info:
        raise HTTPException(status_code=404, detail="Preview not found.")

    # Cancel cleanup task
    task = info.get("cleanup_task")
    if task:
        task.cancel()

    # Remove files
    if info.get("path"):
        shutil.rmtree(info["path"], ignore_errors=True)

    return {"deleted": True, "preview_id": preview_id}
