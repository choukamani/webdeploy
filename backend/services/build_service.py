"""
Build service — run npm install, build with VITE_BASE, and verify with preview.

Uses subprocess for npm/npx commands with proper timeout handling, real-time
stdout/stderr capture, and random port allocation for the preview server.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import subprocess
import time
from typing import Callable, Optional

import httpx

from config import Settings
from models.enums import DeploymentMode

logger = logging.getLogger("webdeploy.build_service")


class BuildService:
    """Run npm install, Vite build, and preview verification."""

    def __init__(
        self,
        log_callback: Optional[Callable] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self._log = log_callback or (lambda msg, **kw: None)
        self._settings = settings or Settings()

    # ── Public API ────────────────────────────────────────────────────

    def install_dependencies(self, source_path: str) -> None:
        """
        Run ``npm install`` in the project directory.

        If the initial install fails due to peer dependency conflicts
        (ERESOLVE), automatically retries with ``--legacy-peer-deps``.

        Raises
        ------
        RuntimeError
            If the install process fails or times out.
        """
        self._log("Running npm install", level="INFO", step="BUILD")
        stdout, stderr, returncode = self._run_command(
            ["npm", "install"],
            cwd=source_path,
            timeout=self._settings.BUILD_TIMEOUT_SECONDS,
        )

        if returncode != 0:
            error_output = stderr or stdout
            # Retry with --legacy-peer-deps when peer dependency conflicts occur
            if "ERESOLVE" in error_output:
                self._log(
                    "Peer dependency conflict detected — retrying with --legacy-peer-deps",
                    level="WARNING",
                    step="BUILD",
                )
                stdout, stderr, returncode = self._run_command(
                    ["npm", "install", "--legacy-peer-deps"],
                    cwd=source_path,
                    timeout=self._settings.BUILD_TIMEOUT_SECONDS,
                )
                if returncode != 0:
                    error_output = stderr or stdout
                    self._log(f"npm install --legacy-peer-deps also failed (exit {returncode})", level="ERROR", step="BUILD")
                    raise RuntimeError(f"npm install failed with exit code {returncode}: {error_output[-1000:]}")
            else:
                self._log(f"npm install failed (exit {returncode})", level="ERROR", step="BUILD")
                self._log(f"stderr: {error_output[-2000:]}", level="ERROR", step="BUILD")
                raise RuntimeError(f"npm install failed with exit code {returncode}: {error_output[-1000:]}")

        self._log("npm install completed successfully", level="INFO", step="BUILD")

    def build(
        self,
        source_path: str,
        website_name: str,
        mode: DeploymentMode,
    ) -> str:
        """
        Run ``npm run build`` with the correct VITE_BASE environment variable.

        Parameters
        ----------
        source_path : str
            Absolute path to the Vite project root.
        website_name : str
            The deployment name (used for demo base path).
        mode : DeploymentMode
            demo or prod.

        Returns
        -------
        str
            Absolute path to the generated dist/ directory.

        Raises
        ------
        RuntimeError
            If the build process fails or times out.
        """
        base_path = f"/{website_name}/" if mode == DeploymentMode.DEMO else "/"
        self._log(f"Building with VITE_BASE={base_path}", level="INFO", step="BUILD")

        # Build environment — inherit current env and add VITE_BASE
        env = os.environ.copy()
        env["VITE_BASE"] = base_path

        stdout, stderr, returncode = self._run_command(
            ["npm", "run", "build"],
            cwd=source_path,
            timeout=self._settings.BUILD_TIMEOUT_SECONDS,
            env=env,
        )

        if returncode != 0:
            error_output = stderr or stdout
            self._log(f"Build failed (exit {returncode})", level="ERROR", step="BUILD")
            self._log(f"stderr: {error_output[-2000:]}", level="ERROR", step="BUILD")
            raise RuntimeError(f"Build failed with exit code {returncode}: {error_output[-1000:]}")

        # Verify dist/ was created
        dist_path = os.path.join(source_path, "dist")
        if not os.path.isdir(dist_path):
            raise RuntimeError(
                "Build completed but dist/ directory was not created. "
                "Ensure the Vite config output directory is 'dist'."
            )

        # Look for index.html — it may be directly in dist/ or in a subdirectory
        # (e.g. dist/public/index.html when outDir is "dist/public")
        index_html = os.path.join(dist_path, "index.html")
        if not os.path.isfile(index_html):
            # Search one level deep for index.html
            found = False
            for entry in os.listdir(dist_path):
                candidate = os.path.join(dist_path, entry, "index.html")
                if os.path.isfile(candidate):
                    dist_path = os.path.join(dist_path, entry)
                    self._log(
                        f"index.html found in dist/{entry}/ — using as build output",
                        level="INFO",
                        step="BUILD",
                    )
                    found = True
                    break
            if not found:
                raise RuntimeError(
                    "Build completed but dist/index.html was not found. "
                    "The build output may be misconfigured."
                )

        self._log("Build completed successfully", level="INFO", step="BUILD")
        return dist_path

    def verify_preview(
        self,
        source_path: str,
        website_name: str,
        mode: DeploymentMode,
    ) -> bool:
        """
        Start ``npx vite preview`` on a random port, verify the app responds
        with HTTP 200, then shut down the preview server.

        Returns
        -------
        bool
            True if verification succeeded.

        Raises
        ------
        RuntimeError
            If the preview server fails to start or returns non-200.
        """
        port = random.randint(10000, 60000)
        base_path = f"/{website_name}/" if mode == DeploymentMode.DEMO else "/"

        if mode == DeploymentMode.DEMO:
            expected_url = f"http://localhost:{port}/{website_name}/"
        else:
            expected_url = f"http://localhost:{port}/"

        self._log(
            f"Starting preview server on port {port} for verification",
            level="INFO",
            step="VERIFY",
        )

        preview_proc: Optional[subprocess.Popen] = None

        try:
            # Start vite preview in background
            env = os.environ.copy()
            env["VITE_BASE"] = base_path

            preview_proc = subprocess.Popen(
                ["npx", "vite", "preview", "--port", str(port)],
                cwd=source_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=(os.name == "nt"),  # Windows needs shell=True to find .cmd scripts
                # Use process group so we can kill the whole tree
                preexec_fn=os.setsid if os.name != "nt" else None,
            )

            # Poll until the server is ready or timeout
            timeout = self._settings.PREVIEW_TIMEOUT_SECONDS
            start_time = time.time()
            server_ready = False

            while time.time() - start_time < timeout:
                # Check if process died
                if preview_proc.poll() is not None:
                    stdout_data = preview_proc.stdout.read().decode("utf-8", errors="replace") if preview_proc.stdout else ""
                    stderr_data = preview_proc.stderr.read().decode("utf-8", errors="replace") if preview_proc.stderr else ""
                    raise RuntimeError(
                        f"Preview server exited prematurely (code {preview_proc.returncode}). "
                        f"stderr: {stderr_data[-500:]}"
                    )

                try:
                    with httpx.Client(timeout=2.0) as client:
                        resp = client.get(expected_url)
                        if resp.status_code == 200:
                            server_ready = True
                            break
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                    pass

                time.sleep(0.5)

            if not server_ready:
                raise RuntimeError(
                    f"Preview server did not respond at {expected_url} "
                    f"within {timeout} seconds"
                )

            self._log(
                f"Preview verification passed — HTTP 200 at {expected_url}",
                level="INFO",
                step="VERIFY",
            )
            return True

        finally:
            # Always clean up the preview server
            if preview_proc is not None and preview_proc.poll() is None:
                self._log("Shutting down preview server", level="INFO", step="VERIFY")
                try:
                    if os.name != "nt":
                        # Kill the entire process group
                        os.killpg(os.getpgid(preview_proc.pid), signal.SIGTERM)
                    else:
                        preview_proc.terminate()
                    preview_proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        if os.name != "nt":
                            os.killpg(os.getpgid(preview_proc.pid), signal.SIGKILL)
                        else:
                            preview_proc.kill()
                        preview_proc.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        logger.warning("Could not kill preview server process %s", preview_proc.pid)

    # ── Private helpers ───────────────────────────────────────────────

    def _run_command(
        self,
        cmd: list[str],
        cwd: str,
        timeout: int,
        env: Optional[dict] = None,
    ) -> tuple[str, str, int]:
        """
        Run a subprocess command with timeout and real-time log streaming.

        Uses a background thread to read stdout/stderr and log lines as they
        arrive, while the main thread waits for the process to finish.
        Returns (stdout, stderr, returncode).
        """
        self._log(f"$ {' '.join(cmd)}", level="INFO", step="BUILD")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=(os.name == "nt"),
            )

            import threading

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def _reader(stream, lines, prefix, level):
                for raw_line in stream:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                    lines.append(line)
                    if line.strip():
                        self._log(f"  {prefix}{line}", level=level, step="BUILD")

            t_out = threading.Thread(
                target=_reader, args=(proc.stdout, stdout_lines, "", "INFO"), daemon=True,
            )
            t_err = threading.Thread(
                target=_reader, args=(proc.stderr, stderr_lines, "[stderr] ", "WARNING"), daemon=True,
            )
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                self._log(
                    f"Command timed out after {timeout}s: {' '.join(cmd)}",
                    level="ERROR",
                    step="BUILD",
                )
                raise RuntimeError(
                    f"Command timed out after {timeout} seconds: {' '.join(cmd)}"
                )

            t_out.join(timeout=5)
            t_err.join(timeout=5)

            return "\n".join(stdout_lines), "\n".join(stderr_lines), proc.returncode

        except RuntimeError:
            raise
        except FileNotFoundError:
            self._log(
                f"Command not found: {cmd[0]}. Ensure Node.js/npm is installed.",
                level="ERROR",
                step="BUILD",
            )
            raise RuntimeError(
                f"Command not found: {cmd[0]}. Ensure Node.js and npm are installed "
                "and available on PATH."
            )
