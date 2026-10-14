"""
ZIP processing service — extract uploaded ZIP, detect Vite project structure, validate.

Handles edge cases such as single-wrapper-folder ZIPs, missing dist/, multiple
dist/ directories, and non-Vite projects.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

from models.deployment import ZipProcessingResult

logger = logging.getLogger("webdeploy.zip_processor")


class ZipProcessingService:
    """Extract and validate an uploaded ZIP containing a Vite project."""

    def __init__(self, log_callback: Optional[Callable] = None) -> None:
        self._log = log_callback or (lambda msg, **kw: None)

    # ── Public API ────────────────────────────────────────────────────

    def process(self, zip_path: str, temp_dir: str) -> ZipProcessingResult:
        """
        Full extraction + validation pipeline.

        Parameters
        ----------
        zip_path : str
            Absolute path to the uploaded .zip file.
        temp_dir : str
            Base temporary directory — a unique sub-folder will be created.

        Returns
        -------
        ZipProcessingResult
            Validated project metadata.

        Raises
        ------
        ValueError
            If the ZIP is invalid or the project structure cannot be resolved.
        """
        self._log("Starting ZIP processing", level="INFO", step="EXTRACT")

        # 1. Validate the ZIP file
        if not os.path.isfile(zip_path):
            raise ValueError(f"ZIP file not found: {zip_path}")

        if not zipfile.is_zipfile(zip_path):
            raise ValueError(f"Not a valid ZIP file: {zip_path}")

        # 2. Create a unique extraction directory
        extract_dir = tempfile.mkdtemp(dir=temp_dir, prefix="zip_extract_")
        self._log(f"Extracting to {extract_dir}", level="INFO", step="EXTRACT")

        try:
            self._extract_zip(zip_path, extract_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Corrupt ZIP file: {exc}") from exc

        # 3. Unwrap single wrapper folder if present
        source_root = self._unwrap_single_folder(extract_dir)
        self._log(f"Project root resolved to: {source_root}", level="INFO", step="EXTRACT")

        # 4. Detect Vite project structure
        detected_issues: list[str] = []
        package_json_path = self._find_package_json_with_vite(source_root)
        if package_json_path is None:
            # Try looking one level deeper
            package_json_path = self._find_package_json_deep(source_root)
            if package_json_path is not None:
                # Adjust source root to the directory containing package.json
                source_root = str(Path(package_json_path).parent)
                self._log(
                    f"Found Vite project in sub-directory: {source_root}",
                    level="WARNING",
                    step="EXTRACT",
                )
            else:
                detected_issues.append("No package.json with Vite dependency found")

        # 5. Parse package.json
        package_json: dict = {}
        has_router = False
        if package_json_path:
            package_json = self._read_package_json(package_json_path)
            has_router = self._detect_router(package_json)
            if has_router:
                self._log("Detected client-side router (react-router / vue-router)", level="INFO", step="EXTRACT")

        # 6. Find vite.config
        vite_config_path = self._find_vite_config(source_root)
        if vite_config_path is None:
            detected_issues.append("No vite.config.js/ts found")
            self._log("No vite.config file detected", level="WARNING", step="EXTRACT")
        else:
            self._log(f"Found vite config: {vite_config_path}", level="INFO", step="EXTRACT")

        # 7. Find dist/ directory
        dist_path = self._find_dist_directory(source_root)
        if dist_path is None:
            detected_issues.append("No dist/ directory with index.html found — build may be required")
            self._log("No dist/ directory found — will need to build", level="WARNING", step="EXTRACT")
            # Use a placeholder; the build step will create dist/
            dist_path = os.path.join(source_root, "dist")
        else:
            self._log(f"Found dist directory: {dist_path}", level="INFO", step="EXTRACT")

        # 8. Validate minimum structure
        if not package_json:
            raise ValueError(
                "Cannot proceed: no package.json with Vite dependency found in the uploaded ZIP. "
                "Ensure the ZIP contains a valid Vite project."
            )

        self._log(
            f"ZIP processing complete — {len(detected_issues)} issue(s) detected",
            level="INFO",
            step="EXTRACT",
        )

        return ZipProcessingResult(
            source_path=source_root,
            dist_path=dist_path,
            vite_config_path=vite_config_path,
            package_json=package_json,
            has_router=has_router,
            detected_issues=detected_issues,
        )

    def process_static(self, zip_path: str, temp_dir: str) -> ZipProcessingResult:
        """
        Extraction pipeline for static HTML/CSS/JS sites (no Vite required).

        Looks for an index.html file and uses its containing directory as the
        dist_path directly.  Skips all Vite-specific validation.

        Parameters
        ----------
        zip_path : str
            Absolute path to the uploaded .zip file.
        temp_dir : str
            Base temporary directory.

        Returns
        -------
        ZipProcessingResult
            Project metadata with is_static=True.

        Raises
        ------
        ValueError
            If the ZIP is invalid or no index.html is found.
        """
        self._log("Starting static site ZIP processing", level="INFO", step="EXTRACT")

        if not os.path.isfile(zip_path):
            raise ValueError(f"ZIP file not found: {zip_path}")
        if not zipfile.is_zipfile(zip_path):
            raise ValueError(f"Not a valid ZIP file: {zip_path}")

        extract_dir = tempfile.mkdtemp(dir=temp_dir, prefix="zip_extract_")
        self._log(f"Extracting to {extract_dir}", level="INFO", step="EXTRACT")

        try:
            self._extract_zip(zip_path, extract_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Corrupt ZIP file: {exc}") from exc

        source_root = self._unwrap_single_folder(extract_dir)
        self._log(f"Project root resolved to: {source_root}", level="INFO", step="EXTRACT")

        # Find HTML files — prefer index.html, fall back to any .html file
        result = self._find_index_html_dir(source_root)
        if result is None:
            raise ValueError(
                "Cannot proceed: no HTML file found in the uploaded ZIP. "
                "Ensure the ZIP contains at least one .html file."
            )

        index_dir, main_html = result
        self._log(
            f"Static site detected — using {index_dir} as deployment root (entry: {main_html})",
            level="INFO",
            step="EXTRACT",
        )

        return ZipProcessingResult(
            source_path=source_root,
            dist_path=index_dir,
            vite_config_path=None,
            package_json={},
            has_router=False,
            is_static=True,
            main_html_file=main_html,
            detected_issues=[],
        )

    def _find_index_html_dir(self, root: str) -> Optional[tuple[str, str]]:
        """Find the directory containing index.html, preferring the root level.
        Falls back to any directory containing at least one .html file.
        Returns (directory, html_filename) or None."""
        # Check root first for index.html
        if os.path.isfile(os.path.join(root, "index.html")):
            return root, "index.html"

        # Search up to 2 levels deep for index.html
        fallback: Optional[tuple[str, str]] = None
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", ".git", "__MACOSX", ".cache")
            ]
            depth = dirpath.replace(root, "").count(os.sep)
            if depth > 2:
                dirnames.clear()
                continue
            if "index.html" in filenames:
                return dirpath, "index.html"
            # Track first directory with any .html file as fallback
            if fallback is None:
                html_files = [f for f in filenames if f.endswith(".html")]
                if html_files:
                    fallback = (dirpath, html_files[0])

        return fallback

    # Marker files that identify a deployable project root for Cloud Run
    _PROJECT_MARKERS = (
        "Dockerfile",
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "index.html",
    )

    def process_generic(self, zip_path: str, temp_dir: str) -> ZipProcessingResult:
        """
        Generic extraction pipeline for Cloud Run mode.

        Same ZIP extraction + unwrap logic as ``process()``, but does NOT require
        Vite in package.json.  Reads package.json if present (optional).

        Parameters
        ----------
        zip_path : str
            Absolute path to the uploaded .zip file.
        temp_dir : str
            Base temporary directory — a unique sub-folder will be created.

        Returns
        -------
        ZipProcessingResult
            Project metadata with source_path set.
        """
        self._log("Starting generic ZIP processing (Cloud Run mode)", level="INFO", step="EXTRACT")

        # 1. Validate the ZIP file
        if not os.path.isfile(zip_path):
            raise ValueError(f"ZIP file not found: {zip_path}")

        if not zipfile.is_zipfile(zip_path):
            raise ValueError(f"Not a valid ZIP file: {zip_path}")

        # 2. Create a unique extraction directory
        extract_dir = tempfile.mkdtemp(dir=temp_dir, prefix="zip_extract_")
        self._log(f"Extracting to {extract_dir}", level="INFO", step="EXTRACT")

        try:
            self._extract_zip(zip_path, extract_dir)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Corrupt ZIP file: {exc}") from exc

        # 3. Unwrap single wrapper folder if present
        source_root = self._unwrap_single_folder(extract_dir)
        self._log(f"Project root resolved to: {source_root}", level="INFO", step="EXTRACT")

        # Debug: log directory contents at source_root
        try:
            root_entries = os.listdir(source_root)
            self._log(
                f"Files at source root: {root_entries}",
                level="INFO",
                step="EXTRACT",
            )
        except OSError as exc:
            self._log(f"Cannot list source root: {exc}", level="ERROR", step="EXTRACT")

        # 4. Detect full-stack projects (backend/ + frontend/) or find project root deeper
        has_marker = self._has_project_marker(source_root)
        self._log(f"Has project marker at root: {has_marker}", level="INFO", step="EXTRACT")
        if not has_marker:
            subdirs = [
                e for e in os.listdir(source_root)
                if os.path.isdir(os.path.join(source_root, e))
                and not e.startswith(".") and not e.startswith("__")
            ]
            has_backend = "backend" in subdirs
            has_frontend = "frontend" in subdirs

            if has_backend and has_frontend:
                # Full-stack project — keep root so both dirs are available
                self._log(
                    f"Detected full-stack project (backend/ + frontend/) — keeping root as source",
                    level="INFO",
                    step="EXTRACT",
                )
            elif has_backend and not has_frontend:
                # Only backend dir — descend into it
                source_root = os.path.join(source_root, "backend")
                self._log(
                    f"Found backend/ directory — using as source root: {source_root}",
                    level="INFO",
                    step="EXTRACT",
                )
            else:
                # Generic deep search for any project marker
                deeper_root = self._find_project_root_deep(source_root)
                if deeper_root:
                    self._log(
                        f"Found project files in sub-directory: {deeper_root}",
                        level="WARNING",
                        step="EXTRACT",
                    )
                    source_root = deeper_root
                else:
                    self._log(
                        "No project marker found in any subdirectory (up to 2 levels deep)",
                        level="WARNING",
                        step="EXTRACT",
                    )

        # 5. Optionally read package.json (no Vite requirement)
        detected_issues: list[str] = []
        package_json: dict = {}
        has_router = False
        package_json_path = os.path.join(source_root, "package.json")
        if os.path.isfile(package_json_path):
            package_json = self._read_package_json(package_json_path)
            has_router = self._detect_router(package_json)
            self._log("Found package.json", level="INFO", step="EXTRACT")
        else:
            self._log("No package.json at root (may be a Python or static project)", level="INFO", step="EXTRACT")

        # 6. dist_path placeholder (Cloud Run builds via Docker, not Vite)
        dist_path = os.path.join(source_root, "dist")

        self._log(
            f"Generic ZIP processing complete — {len(detected_issues)} issue(s) detected",
            level="INFO",
            step="EXTRACT",
        )

        return ZipProcessingResult(
            source_path=source_root,
            dist_path=dist_path,
            vite_config_path=None,
            package_json=package_json,
            has_router=has_router,
            detected_issues=detected_issues,
        )

    # ── Private helpers ───────────────────────────────────────────────

    def _has_project_marker(self, directory: str) -> bool:
        """Check whether *directory* contains any recognised project marker file."""
        return any(
            os.path.isfile(os.path.join(directory, m))
            for m in self._PROJECT_MARKERS
        )

    def _find_project_root_deep(self, root: str, max_depth: int = 2) -> Optional[str]:
        """
        Search up to *max_depth* levels below *root* for a directory that
        contains a recognised project marker file.  Returns the first match
        or ``None``.
        """
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", ".git", "dist", "build", "__pycache__", "__MACOSX")
            ]
            depth = dirpath.replace(root, "").count(os.sep)
            if depth > max_depth:
                dirnames.clear()
                continue
            if depth == 0:
                # Already checked root level
                continue
            for marker in self._PROJECT_MARKERS:
                if marker in filenames:
                    return dirpath
        return None

    def _extract_zip(self, zip_path: str, dest: str) -> None:
        """Safely extract a ZIP file, guarding against path traversal attacks."""
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                # Guard against zip-slip / path traversal
                member_path = os.path.realpath(os.path.join(dest, member))
                if not member_path.startswith(os.path.realpath(dest)):
                    raise ValueError(f"Zip entry attempts path traversal: {member}")
            zf.extractall(dest)
        self._log(f"Extracted {len(zf.namelist())} entries from ZIP", level="INFO", step="EXTRACT")

    def _unwrap_single_folder(self, extract_dir: str) -> str:
        """
        If the extracted content is a single top-level directory (common when
        zipping a folder), return the inner directory as the project root.
        """
        entries = [
            e for e in os.listdir(extract_dir)
            if not e.startswith("__MACOSX") and not e.startswith(".")
        ]
        if len(entries) == 1:
            candidate = os.path.join(extract_dir, entries[0])
            if os.path.isdir(candidate):
                self._log(
                    f"Detected single wrapper folder '{entries[0]}' — unwrapping",
                    level="INFO",
                    step="EXTRACT",
                )
                return candidate
        return extract_dir

    def _find_package_json_with_vite(self, root: str) -> Optional[str]:
        """Look for package.json at the project root containing a Vite dependency."""
        candidate = os.path.join(root, "package.json")
        if os.path.isfile(candidate):
            try:
                data = json.loads(Path(candidate).read_text(encoding="utf-8"))
                if self._has_vite_dependency(data):
                    return candidate
            except (json.JSONDecodeError, OSError) as exc:
                self._log(f"Failed to parse {candidate}: {exc}", level="WARNING", step="EXTRACT")
        return None

    def _find_package_json_deep(self, root: str) -> Optional[str]:
        """Search up to 2 levels deep for a package.json with Vite."""
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip node_modules, .git, dist, build
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", ".git", "dist", "build", "__MACOSX")
            ]
            # Limit depth to 2 levels
            depth = dirpath.replace(root, "").count(os.sep)
            if depth > 2:
                dirnames.clear()
                continue

            if "package.json" in filenames:
                candidate = os.path.join(dirpath, "package.json")
                try:
                    data = json.loads(Path(candidate).read_text(encoding="utf-8"))
                    if self._has_vite_dependency(data):
                        return candidate
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    @staticmethod
    def _has_vite_dependency(package_json: dict) -> bool:
        """Check whether vite appears in dependencies or devDependencies."""
        all_deps: dict = {}
        all_deps.update(package_json.get("dependencies", {}))
        all_deps.update(package_json.get("devDependencies", {}))
        return "vite" in all_deps

    @staticmethod
    def _read_package_json(path: str) -> dict:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _detect_router(package_json: dict) -> bool:
        """Detect common client-side routers."""
        all_deps: dict = {}
        all_deps.update(package_json.get("dependencies", {}))
        all_deps.update(package_json.get("devDependencies", {}))
        router_packages = {"react-router-dom", "react-router", "vue-router", "@tanstack/react-router"}
        return bool(router_packages & set(all_deps.keys()))

    def _find_vite_config(self, root: str) -> Optional[str]:
        """Find vite.config.{js,ts,mjs,mts} at the project root."""
        candidates = [
            "vite.config.js",
            "vite.config.ts",
            "vite.config.mjs",
            "vite.config.mts",
        ]
        for name in candidates:
            path = os.path.join(root, name)
            if os.path.isfile(path):
                return path
        return None

    def _find_dist_directory(self, root: str) -> Optional[str]:
        """
        Find dist/ directory containing index.html.
        Prefers a dist/ at the project root; falls back to any dist/ up to
        2 levels deep.
        """
        # Check root-level dist first
        root_dist = os.path.join(root, "dist")
        if os.path.isdir(root_dist) and os.path.isfile(os.path.join(root_dist, "index.html")):
            return root_dist

        # Search deeper
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("node_modules", ".git", "__MACOSX")
            ]
            depth = dirpath.replace(root, "").count(os.sep)
            if depth > 2:
                dirnames.clear()
                continue
            if os.path.basename(dirpath) == "dist" and "index.html" in filenames:
                return dirpath

        return None
