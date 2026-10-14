"""
AI validation service: inspect a Vite project's source for deployment-breaking
issues (wrong base path, absolute asset URLs, router basename...) and apply the
fixes the model proposes.

The model is reached through ``services.llm_providers``: providers are tried in
the order configured by ``AI_PROVIDERS`` (e.g. Gemini, then an OpenAI-compatible
API, then OpenRouter), each with retries on transient errors. If none answers,
the pipeline continues without AI inspection instead of failing the deployment.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from config import Settings
from models.deployment import AIValidationResult
from models.enums import DeploymentMode
from services.llm_providers import LLMProvider, ProviderAuthError, ProviderError, build_providers

logger = logging.getLogger("webdeploy.ai_validator")

# File extensions to collect for inspection
_SOURCE_EXTENSIONS = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".css", ".html",
})

# Files to always include by name (at any directory level)
_INCLUDE_NAMES = frozenset({
    "vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.mts",
    "package.json",
})

# Directories to skip entirely
_SKIP_DIRS = frozenset({
    "node_modules", "dist", ".git", "build", "__MACOSX", ".cache",
    ".vite", ".next",
})

# Maximum individual file size sent to the model (256 KB)
_MAX_FILE_BYTES = 256 * 1024

# Maximum total payload size (~400 KB of source code text)
_MAX_TOTAL_BYTES = 400 * 1024

# Retry settings, per provider
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2  # seconds


class AIValidationService:
    """Use an LLM to inspect and auto-fix Vite project source code."""

    def __init__(
        self,
        log_callback: Optional[Callable] = None,
        settings: Optional[Settings] = None,
        providers: Optional[list[LLMProvider]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._log = log_callback or (lambda msg, **kw: None)
        self._settings = settings or Settings()
        self._providers = providers if providers is not None else build_providers(self._settings)
        self._sleep = sleep

    # ── Public API ────────────────────────────────────────────────────

    def validate_and_fix(
        self,
        source_path: str,
        website_name: str,
        mode: DeploymentMode,
        has_router: bool = False,
    ) -> AIValidationResult:
        """
        Inspect source code with the configured LLM and apply any suggested fixes.

        Parameters
        ----------
        source_path : str
            Absolute path to the Vite project root (contains package.json).
        website_name : str
            Deployment name used for base-path calculation.
        mode : DeploymentMode
            demo or prod — determines the target base path.
        has_router : bool
            Whether a client-side router was detected.
        """
        self._log("Starting AI validation", level="INFO", step="AI_INSPECT")

        # 1. Collect source files
        files = self._collect_source_files(source_path)
        if not files:
            self._log("No source files found to inspect", level="WARNING", step="AI_INSPECT")
            return AIValidationResult(
                status="pass",
                summary="No source files found — skipping AI inspection.",
            )

        self._log(f"Collected {len(files)} file(s) for inspection", level="INFO", step="AI_INSPECT")

        # 2. Determine base path
        base_path = f"/{website_name}/" if mode == DeploymentMode.DEMO else "/"
        self._log(f"Target base path: {base_path}", level="INFO", step="AI_INSPECT")

        # 3. Build prompt
        prompt = self._build_prompt(files, base_path, website_name, mode, has_router)

        # 4. Ask the providers in order; the first one that answers wins
        response_text, token_usage = self._call_providers(prompt)

        # 4b. If no provider answered, skip validation rather than failing the deployment
        if not response_text:
            return AIValidationResult(
                status="pass",
                summary="AI validation skipped (no AI provider available) — proceeding without inspection.",
            )

        # 5. Parse response
        result = self._parse_response(response_text)
        self._log(
            f"AI found {len(result.issues_found)} issue(s), suggested {len(result.fixes)} fix(es)",
            level="INFO",
            step="AI_INSPECT",
        )

        # 6. Apply fixes
        if result.fixes:
            self._log("Applying AI-suggested fixes", level="INFO", step="AI_FIX")
            applied_count = self._apply_fixes(source_path, result.fixes)
            self._log(f"Applied {applied_count}/{len(result.fixes)} fix(es)", level="INFO", step="AI_FIX")

        # 7. Attach token usage for cost tracking
        if token_usage:
            result.token_usage = token_usage
            self._log(
                f"Token usage ({token_usage.get('provider')}) — input: "
                f"{token_usage.get('input_tokens', 0)}, output: {token_usage.get('output_tokens', 0)}",
                level="INFO",
                step="AI_INSPECT",
            )

        return result

    # ── File collection ───────────────────────────────────────────────

    def _collect_source_files(self, source_path: str) -> dict[str, str]:
        """Walk the source tree and return {relative_path: file_content} for relevant files."""
        files: dict[str, str] = {}
        total_bytes = 0

        for dirpath, dirnames, filenames in os.walk(source_path):
            # Prune skippable directories
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, source_path)

                ext = os.path.splitext(filename)[1].lower()
                if ext not in _SOURCE_EXTENSIONS and filename not in _INCLUDE_NAMES:
                    continue

                try:
                    size = os.path.getsize(abs_path)
                except OSError:
                    continue

                if size > _MAX_FILE_BYTES:
                    self._log(
                        f"Skipping large file: {rel_path} ({size // 1024} KB)",
                        level="WARNING",
                        step="AI_INSPECT",
                    )
                    continue

                if total_bytes + size > _MAX_TOTAL_BYTES:
                    self._log(
                        "Reached payload size limit — truncating file collection",
                        level="WARNING",
                        step="AI_INSPECT",
                    )
                    break

                try:
                    files[rel_path] = Path(abs_path).read_text(encoding="utf-8", errors="replace")
                    total_bytes += size
                except OSError as exc:
                    self._log(f"Could not read {rel_path}: {exc}", level="WARNING", step="AI_INSPECT")

        return files

    # ── Prompt construction ───────────────────────────────────────────

    def _build_prompt(
        self,
        files: dict[str, str],
        base_path: str,
        website_name: str,
        mode: DeploymentMode,
        has_router: bool,
    ) -> str:
        """Build the detailed inspection prompt sent to the model."""
        file_listing = ""
        for rel_path, content in files.items():
            file_listing += f"\n--- FILE: {rel_path} ---\n{content}\n"

        router_note = ""
        if has_router:
            router_note = """
- ROUTER BASENAME: The project uses a client-side router. Ensure the router
  basename is set to "{base_path}". For react-router-dom, this means
  <BrowserRouter basename="{base_path}"> or equivalent. For vue-router, set
  the base option in createRouter.
""".format(base_path=base_path)

        return f"""You are an expert Vite deployment engineer. You are inspecting a Vite project
that will be deployed with base path: "{base_path}"

Deployment mode: {mode.value}
Website name: {website_name}
Target base path: {base_path}

Your task: Identify ALL deployment-breaking issues and provide exact fixes.

## INSPECTION CHECKLIST

1. **vite.config base path**: The `base` property in vite.config must be set to
   exactly "{base_path}". If it is missing, set to "/" when it should be
   "{base_path}", or set to a wrong value, this is a critical fix.

2. **Asset references**: Any hardcoded absolute paths in source code like
   `/assets/`, `/images/`, `/favicon.ico` must be prefixed with the base path
   or use relative paths / import statements.

3. **index.html paths**: In index.html, ensure script src and link href
   attributes either use relative paths or start with "{base_path}".

4. **CSS url() paths**: In CSS files, check for `url(/...)` absolute
   references that will break under a sub-path deployment.

5. **Navigation links**: Hardcoded `href="/"` or `to="/"` links should
   use the base path.

6. **package.json scripts**: The build script should ideally not hardcode
   a different base. Check for `--base` flags.
{router_note}
## SOURCE FILES

{file_listing}

## RESPONSE FORMAT

You MUST respond with ONLY valid JSON, no markdown fences, no explanation
outside the JSON. Use this exact schema:

{{
  "status": "pass" | "needs_fixes",
  "issues": [
    {{
      "file": "relative/path/to/file",
      "line": 10,
      "severity": "critical" | "warning",
      "description": "What is wrong"
    }}
  ],
  "fixes": [
    {{
      "file": "relative/path/to/file",
      "description": "What this fix does",
      "original": "exact original text to find",
      "replacement": "exact replacement text"
    }}
  ],
  "summary": "Brief human-readable summary of the inspection"
}}

If everything looks correct, return status "pass" with empty issues and fixes
arrays and a summary saying the project is ready for deployment.
"""

    # ── Provider calls ────────────────────────────────────────────────

    def _call_providers(self, prompt: str) -> tuple[str, dict]:
        """Try each provider in order, with retries; return (text, usage) or ("", {})."""
        if not self._providers:
            self._log(
                "No AI provider configured (AI_PROVIDERS and API keys) — skipping AI validation",
                level="WARNING",
                step="AI_INSPECT",
            )
            return "", {}
        for provider in self._providers:
            text, usage = self._call_with_retries(provider, prompt)
            if text:
                return text, usage
        self._log("No AI provider answered — skipping AI validation", level="WARNING", step="AI_INSPECT")
        return "", {}

    def _call_with_retries(self, provider: LLMProvider, prompt: str) -> tuple[str, dict]:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._log(
                    f"Calling {provider.name} ({provider.model}), attempt {attempt}/{_MAX_RETRIES}",
                    level="INFO",
                    step="AI_INSPECT",
                )
                reply = provider.complete(prompt, max_tokens=4096)
                self._log(f"{provider.name} call succeeded", level="INFO", step="AI_INSPECT")
                return reply.text, reply.usage
            except ProviderAuthError as exc:
                # Bad key or no credit: retrying cannot help. Alert, then try the next provider.
                self._log(f"{provider.name}: {exc} — trying the next provider", level="WARNING",
                          step="AI_INSPECT")
                self._send_api_key_alert(provider.name, exc.status_code or 0, str(exc))
                return "", {}
            except ProviderError as exc:
                if not exc.retryable or attempt == _MAX_RETRIES:
                    self._log(f"{provider.name} unavailable: {exc}", level="WARNING", step="AI_INSPECT")
                    return "", {}
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                self._log(f"{provider.name}: {exc} — retrying in {delay}s", level="WARNING",
                          step="AI_INSPECT")
                self._sleep(delay)
        return "", {}

    # ── Response parsing ──────────────────────────────────────────────

    def _parse_response(self, response_text: str) -> AIValidationResult:
        """Parse the model's JSON response into an AIValidationResult."""
        text = response_text.strip()
        if text.startswith("```"):
            # Remove the opening fence (```json or ```) and the closing one
            lines = text.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._log(f"Failed to parse the AI response as JSON: {exc}", level="ERROR", step="AI_INSPECT")
            logger.debug("Raw AI response: %s", response_text[:2000])
            return AIValidationResult(
                status="pass",
                summary="AI response could not be parsed — proceeding without fixes.",
            )

        return AIValidationResult(
            status=data.get("status", "pass"),
            issues_found=data.get("issues", []),
            fixes=data.get("fixes", []),
            summary=data.get("summary", ""),
        )

    # ── Fix application ───────────────────────────────────────────────

    def _apply_fixes(self, source_path: str, fixes: list[dict]) -> int:
        """
        Apply the fixes suggested by the model. Each fix is a dict with ``file``
        (relative path), ``original`` (text to find), ``replacement`` and
        ``description``. Returns the number of successfully applied fixes.
        """
        applied = 0
        real_source = os.path.realpath(source_path)

        for fix in fixes:
            rel_path = fix.get("file", "")
            original = fix.get("original", "")
            replacement = fix.get("replacement", "")
            description = fix.get("description", "")

            if not rel_path or not original:
                self._log(f"Skipping incomplete fix: {description}", level="WARNING", step="AI_FIX")
                continue

            abs_path = os.path.join(source_path, rel_path)

            # Security: model output is untrusted; the target must stay inside the source tree.
            # commonpath (not startswith) so that "/src-evil" is not accepted for "/src".
            real_target = os.path.realpath(abs_path)
            if os.path.commonpath([real_source, real_target]) != real_source:
                self._log(f"Fix target escapes source directory — skipping: {rel_path}",
                          level="ERROR", step="AI_FIX")
                continue

            if not os.path.isfile(abs_path):
                self._log(f"Fix target file not found — skipping: {rel_path}", level="WARNING",
                          step="AI_FIX")
                continue

            try:
                content = Path(abs_path).read_text(encoding="utf-8")
                if original not in content:
                    self._log(
                        f"Original text not found in {rel_path} — skipping fix: {description}",
                        level="WARNING",
                        step="AI_FIX",
                    )
                    continue
                Path(abs_path).write_text(content.replace(original, replacement, 1), encoding="utf-8")
                self._log(f"Fixed {rel_path}: {description}", level="INFO", step="AI_FIX")
                applied += 1
            except OSError as exc:
                self._log(f"Failed to apply fix to {rel_path}: {exc}", level="ERROR", step="AI_FIX")

        return applied

    # ── API key alert notification ─────────────────────────────────────

    def _send_api_key_alert(self, provider: str, status_code: int, error_message: str) -> None:
        """
        Email AI_ALERT_EMAILS when a provider key is invalid, unauthorised or out
        of credit (401/402/403). Does nothing if no recipient is configured.
        """
        recipients = [e.strip() for e in self._settings.AI_ALERT_EMAILS.split(",") if e.strip()]
        if not recipients:
            return
        try:
            import asyncio

            from services.email_service import EmailService

            status_labels = {
                401: "Invalid / Expired API Key",
                402: "Insufficient Credits (Payment Required)",
                403: "Access Denied / Forbidden",
            }
            reason = status_labels.get(status_code, f"HTTP {status_code}")
            service = EmailService(settings=self._settings)

            async def _send():
                await service.send_notification(
                    website_name=f"[SYSTEM ALERT] AI provider {provider}",
                    mode="alert",
                    success=False,
                    error_message=(
                        f"AI provider {provider} error: {reason}\n\n"
                        f"HTTP Status: {status_code}\n"
                        f"Error Details: {error_message}\n\n"
                        "Other configured providers are tried; if none works, the AI "
                        "validation step is skipped until the key is fixed."
                    ),
                    ai_summary=None,
                    recipients=recipients,
                )

            # We run in a worker thread: schedule on the running loop, or run one-off.
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(asyncio.ensure_future, _send())
            except RuntimeError:
                asyncio.run(_send())

            self._log(f"API key alert email sent to {', '.join(recipients)}", level="WARNING",
                      step="AI_INSPECT")
        except Exception as exc:
            logger.warning("Failed to send API key alert email: %s", exc)
