"""
Dockerfile generator — detect project type and generate appropriate Dockerfile.

Supports Node.js, Python, and static Vite projects.  All generated Dockerfiles
expose port 8080 and set ``ENV PORT=8080`` as required by Cloud Run.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("webdeploy.dockerfile_generator")


class DockerfileGenerator:
    """Detect project type and generate a Dockerfile for Cloud Run deployment."""

    def __init__(self, log_callback: Optional[Callable] = None) -> None:
        self._log = log_callback or (lambda msg, **kw: None)

    def detect_and_generate(
        self, source_path: str, *, fallback_to_static: bool = False,
    ) -> tuple[str, str]:
        """
        Detect the project type and generate an appropriate Dockerfile.

        Detection priority:
        1. Existing Dockerfile (use as-is)
        2. package.json with vite dependency (static Vite — multi-stage nginx)
        3. package.json with start script (Node.js)
        4. requirements.txt or pyproject.toml (Python)
        5. Any .html file (static HTML)
        6. (if *fallback_to_static*) Serve everything via nginx as a last resort

        Parameters
        ----------
        source_path : str
            Absolute path to the extracted project root.
        fallback_to_static : bool
            If ``True``, fall back to a generic nginx static-file Dockerfile
            instead of raising ``ValueError`` when the project type cannot be
            determined.  Used by Cloud Run mode where the project type is
            unknown ahead of time.

        Returns
        -------
        tuple[str, str]
            ``(project_type, dockerfile_content)``.

        Raises
        ------
        ValueError
            If the project type cannot be determined and *fallback_to_static*
            is ``False``.
        """
        # Log directory contents for debugging
        try:
            entries = os.listdir(source_path)
            self._log(
                f"Source path: {source_path} — contents: {entries}",
                level="INFO",
                step="BUILD",
            )
        except OSError as exc:
            self._log(
                f"Cannot list source path {source_path}: {exc}",
                level="ERROR",
                step="BUILD",
            )

        # 1. Existing Dockerfile
        dockerfile_path = os.path.join(source_path, "Dockerfile")
        if os.path.isfile(dockerfile_path):
            self._log("Found existing Dockerfile — using as-is", level="INFO", step="BUILD")
            content = Path(dockerfile_path).read_text(encoding="utf-8")
            return "existing", content

        # 1b. Full-stack project (backend/ + frontend/ directories)
        backend_dir = os.path.join(source_path, "backend")
        frontend_dir = os.path.join(source_path, "frontend")
        if os.path.isdir(backend_dir) and os.path.isdir(frontend_dir):
            result = self._detect_fullstack(source_path, backend_dir, frontend_dir)
            if result:
                return result

        # 2. Check for package.json
        package_json_path = os.path.join(source_path, "package.json")
        if os.path.isfile(package_json_path):
            try:
                pkg = json.loads(Path(package_json_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pkg = {}

            # 2a. Vite project (static)
            all_deps = {}
            all_deps.update(pkg.get("dependencies", {}))
            all_deps.update(pkg.get("devDependencies", {}))

            has_lockfile = os.path.isfile(os.path.join(source_path, "package-lock.json"))
            if "vite" in all_deps:
                self._log("Detected static Vite project — generating nginx Dockerfile", level="INFO", step="BUILD")
                content = self._template_static_vite(has_lockfile=has_lockfile)
                self._write_dockerfile(source_path, content)
                return "static_vite", content

            # 2b. Node.js with start script
            has_lockfile = os.path.isfile(os.path.join(source_path, "package-lock.json"))
            scripts = pkg.get("scripts", {})
            if "start" in scripts:
                self._log(
                    f"Detected Node.js project (start: {scripts['start']}) — generating Node.js Dockerfile",
                    level="INFO",
                    step="BUILD",
                )
                content = self._template_nodejs(has_lockfile=has_lockfile)
                self._write_dockerfile(source_path, content)
                return "nodejs", content

            # 2c. Node.js without start script — detect entry point and add one
            entrypoint = self._detect_node_entrypoint(source_path, pkg)
            if entrypoint:
                self._log(
                    f"Detected Node.js project (no start script, entrypoint: {entrypoint}) — generating Dockerfile",
                    level="INFO",
                    step="BUILD",
                )
                content = self._template_nodejs_with_entrypoint(entrypoint, has_lockfile=has_lockfile)
                self._write_dockerfile(source_path, content)
                return "nodejs", content

        # 3. Python project
        has_requirements = os.path.isfile(os.path.join(source_path, "requirements.txt"))
        has_pyproject = os.path.isfile(os.path.join(source_path, "pyproject.toml"))

        if has_requirements or has_pyproject:
            entrypoint = self._detect_python_entrypoint(source_path)
            self._log(
                f"Detected Python project — entrypoint: {entrypoint}",
                level="INFO",
                step="BUILD",
            )
            content = self._template_python(
                has_requirements=has_requirements,
                entrypoint=entrypoint,
            )
            self._write_dockerfile(source_path, content)
            return "python", content

        # 4. Static HTML site (index.html or any .html file)
        has_index_html = os.path.isfile(os.path.join(source_path, "index.html"))
        has_any_html = any(
            f.endswith(".html")
            for f in os.listdir(source_path)
            if os.path.isfile(os.path.join(source_path, f))
        )

        if has_index_html or has_any_html:
            self._log("Detected static HTML site — generating nginx Dockerfile", level="INFO", step="BUILD")
            content = self._template_static_html()
            self._write_dockerfile(source_path, content)
            return "static_html", content

        # 5. Fallback: serve everything as static files via nginx (Cloud Run)
        if fallback_to_static:
            self._log(
                "Could not detect project type — falling back to generic nginx static file server",
                level="WARNING",
                step="BUILD",
            )
            content = self._template_static_html()
            self._write_dockerfile(source_path, content)
            return "static_fallback", content

        raise ValueError(
            "Cannot determine project type. The project must contain one of: "
            "Dockerfile, package.json (with vite or start script), "
            "requirements.txt, pyproject.toml, or an index.html file."
        )

    # ── Templates ──────────────────────────────────────────────────────

    @staticmethod
    def _template_nodejs(has_lockfile: bool = True) -> str:
        install_cmd = "RUN npm ci --only=production" if has_lockfile else "RUN npm install --only=production"
        return f"""\
FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
{install_cmd}

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["npm", "start"]
"""

    @staticmethod
    def _template_static_vite(has_lockfile: bool = True) -> str:
        install_cmd = "RUN npm ci" if has_lockfile else "RUN npm install"
        # Build the nginx printf as one line (\\n in Python → \n in output,
        # which printf interprets as newline)
        nginx_conf = (
            "RUN printf 'server {\\n"
            "    listen 8080;\\n"
            "    server_name _;\\n"
            "    root /usr/share/nginx/html;\\n"
            "    index index.html;\\n"
            "    location / {\\n"
            "        try_files $uri $uri/ /index.html;\\n"
            "    }\\n"
            "}\\n' > /etc/nginx/conf.d/default.conf"
        )
        return (
            "# Stage 1: Build\n"
            "FROM node:20-alpine AS build\n"
            "\n"
            "WORKDIR /app\n"
            "\n"
            "COPY package*.json ./\n"
            "COPY tsconfig*.json ./\n"
            + install_cmd + "\n"
            "\n"
            "COPY . .\n"
            "\n"
            "# Build: try npm run build first, fall back to direct vite build\n"
            "# (skips tsc type-checking that may fail on strict projects)\n"
            "ENV NODE_ENV=production\n"
            "RUN npm run build 2>&1 || npx vite build 2>&1\n"
            "\n"
            "# Stage 2: Serve with nginx\n"
            "FROM nginx:alpine\n"
            "\n"
            "COPY --from=build /app/dist /usr/share/nginx/html\n"
            "\n"
            "# nginx config for SPA + Cloud Run port\n"
            + nginx_conf + "\n"
            "\n"
            "ENV PORT=8080\n"
            "EXPOSE 8080\n"
            "\n"
            'CMD ["nginx", "-g", "daemon off;"]\n'
        )

    @staticmethod
    def _template_static_html() -> str:
        return """\
FROM nginx:alpine

COPY . /usr/share/nginx/html

# nginx config for SPA + Cloud Run port
RUN printf 'server {\\n\
    listen 8080;\\n\
    server_name _;\\n\
    root /usr/share/nginx/html;\\n\
    index index.html;\\n\
    location / {\\n\
        try_files $uri $uri/ /index.html;\\n\
    }\\n\
}\\n' > /etc/nginx/conf.d/default.conf

ENV PORT=8080
EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
"""

    @staticmethod
    def _template_python(has_requirements: bool, entrypoint: str) -> str:
        install_cmd = (
            "COPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt"
            if has_requirements
            else "COPY pyproject.toml .\nRUN pip install --no-cache-dir ."
        )

        return f"""\
FROM python:3.11-slim

WORKDIR /app

{install_cmd}

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD {entrypoint}
"""

    @staticmethod
    def _template_nodejs_with_entrypoint(entrypoint: str, has_lockfile: bool = True) -> str:
        install_cmd = "RUN npm ci --only=production" if has_lockfile else "RUN npm install --only=production"
        return f"""\
FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
{install_cmd}

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["node", "{entrypoint}"]
"""

    # ── Full-stack helpers ─────────────────────────────────────────────

    def _detect_fullstack(
        self, source_path: str, backend_dir: str, frontend_dir: str,
    ) -> Optional[tuple[str, str]]:
        """Detect and generate Dockerfile for backend/ + frontend/ projects."""
        # Determine backend type
        backend_reqs = os.path.join(backend_dir, "requirements.txt")
        backend_pyproject = os.path.join(backend_dir, "pyproject.toml")
        backend_pkg = os.path.join(backend_dir, "package.json")

        # Determine frontend type
        frontend_pkg = os.path.join(frontend_dir, "package.json")
        has_frontend_pkg = os.path.isfile(frontend_pkg)

        # Python backend + JS frontend (most common full-stack pattern)
        if os.path.isfile(backend_reqs) or os.path.isfile(backend_pyproject):
            entrypoint = self._detect_python_entrypoint(backend_dir)
            has_reqs = os.path.isfile(backend_reqs)

            if has_frontend_pkg:
                self._log(
                    "Detected full-stack project: Python backend + JS frontend",
                    level="INFO",
                    step="BUILD",
                )
                content = self._template_fullstack_python_js(
                    has_requirements=has_reqs,
                    entrypoint=entrypoint,
                )
            else:
                self._log(
                    "Detected full-stack project: Python backend (no JS frontend build needed)",
                    level="INFO",
                    step="BUILD",
                )
                content = self._template_fullstack_python_only(
                    has_requirements=has_reqs,
                    entrypoint=entrypoint,
                )
            self._write_dockerfile(source_path, content)
            return "fullstack_python", content

        # Node.js backend + JS frontend
        if os.path.isfile(backend_pkg):
            self._log(
                "Detected full-stack project: Node.js backend + JS frontend",
                level="INFO",
                step="BUILD",
            )
            content = self._template_fullstack_node_js()
            self._write_dockerfile(source_path, content)
            return "fullstack_nodejs", content

        return None

    @staticmethod
    def _template_fullstack_python_js(has_requirements: bool, entrypoint: str) -> str:
        install_cmd = (
            "COPY backend/requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt"
            if has_requirements
            else "COPY backend/pyproject.toml .\nRUN pip install --no-cache-dir ."
        )

        return f"""\
# Stage 1: Build frontend
FROM node:20-alpine AS frontend-build

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

# Stage 2: Python backend serving frontend static files
FROM python:3.11-slim

WORKDIR /app

{install_cmd}

COPY backend/ .
COPY --from=frontend-build /frontend/dist /app/static

ENV PORT=8080
EXPOSE 8080

CMD {entrypoint}
"""

    @staticmethod
    def _template_fullstack_python_only(has_requirements: bool, entrypoint: str) -> str:
        install_cmd = (
            "COPY backend/requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt"
            if has_requirements
            else "COPY backend/pyproject.toml .\nRUN pip install --no-cache-dir ."
        )

        return f"""\
FROM python:3.11-slim

WORKDIR /app

{install_cmd}

COPY backend/ .
COPY frontend/ /app/static

ENV PORT=8080
EXPOSE 8080

CMD {entrypoint}
"""

    @staticmethod
    def _template_fullstack_node_js() -> str:
        return """\
# Stage 1: Build frontend
FROM node:20-alpine AS frontend-build

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build

# Stage 2: Node.js backend
FROM node:20-alpine

WORKDIR /app

COPY backend/package*.json ./
RUN npm ci --only=production

COPY backend/ .
COPY --from=frontend-build /frontend/dist /app/static

ENV PORT=8080
EXPOSE 8080

CMD ["npm", "start"]
"""

    # ── Helpers ────────────────────────────────────────────────────────

    def _detect_node_entrypoint(self, source_path: str, pkg: dict) -> Optional[str]:
        """Detect Node.js entry point when no start script is defined."""
        # Check "main" field in package.json
        main_field = pkg.get("main")
        if main_field and os.path.isfile(os.path.join(source_path, main_field)):
            return main_field

        # Check common entry file names
        for candidate in ("server.js", "index.js", "app.js", "main.js"):
            if os.path.isfile(os.path.join(source_path, candidate)):
                return candidate

        return None

    def _detect_python_entrypoint(self, source_path: str) -> str:
        """Auto-detect the Python entrypoint command."""
        # Check for common frameworks
        requirements_path = os.path.join(source_path, "requirements.txt")
        requirements_content = ""
        if os.path.isfile(requirements_path):
            try:
                requirements_content = Path(requirements_path).read_text(encoding="utf-8").lower()
            except OSError:
                pass

        # Check for common app files
        has_app_py = os.path.isfile(os.path.join(source_path, "app.py"))
        has_main_py = os.path.isfile(os.path.join(source_path, "main.py"))

        # FastAPI detection
        if "fastapi" in requirements_content or "uvicorn" in requirements_content:
            module = "main:app" if has_main_py else "app:app"
            return f'["uvicorn", "{module}", "--host", "0.0.0.0", "--port", "8080"]'

        # Flask detection
        if "flask" in requirements_content:
            module = "app" if has_app_py else "main"
            return f'["python", "-m", "flask", "--app", "{module}", "run", "--host", "0.0.0.0", "--port", "8080"]'

        # Gunicorn detection
        if "gunicorn" in requirements_content:
            module = "app:app" if has_app_py else "main:app"
            return f'["gunicorn", "--bind", "0.0.0.0:8080", "{module}"]'

        # Fallback: try to run main.py or app.py directly
        if has_main_py:
            return '["python", "main.py"]'
        if has_app_py:
            return '["python", "app.py"]'

        return '["python", "app.py"]'

    @staticmethod
    def _write_dockerfile(source_path: str, content: str) -> None:
        """Write the generated Dockerfile to the source directory."""
        dockerfile_path = os.path.join(source_path, "Dockerfile")
        Path(dockerfile_path).write_text(content, encoding="utf-8")
        logger.info("Generated Dockerfile at %s", dockerfile_path)
