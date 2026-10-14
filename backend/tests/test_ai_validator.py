"""AI validation: provider adapters (mocked HTTP), fallback chain, retries, safe fix application."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from config import Settings
from models.enums import DeploymentMode
from services.ai_validator import AIValidationService
from services.llm_providers import (
    GeminiProvider,
    LLMReply,
    OpenAICompatibleProvider,
    ProviderAuthError,
    ProviderError,
    build_providers,
)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── Provider adapters ────────────────────────────────────────────────────


def test_openai_compatible_request_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"status": "pass"}'}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 8},
        })

    provider = OpenAICompatibleProvider("openrouter", "https://llm.test/v1", "sk-test", "m",
                                        client=mock_client(handler))
    reply = provider.complete("inspect this")
    assert reply.text == '{"status": "pass"}'
    assert reply.usage == {"provider": "openrouter", "model": "m",
                           "input_tokens": 120, "output_tokens": 8}
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["temperature"] == 0


def test_gemini_request_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models/gemini-x:generateContent")
        assert request.headers["x-goog-api-key"] == "g-key"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"status":'}, {"text": ' "pass"}'}]}}],
            "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 5},
        })

    reply = GeminiProvider("g-key", "gemini-x", client=mock_client(handler)).complete("p")
    assert json.loads(reply.text) == {"status": "pass"}
    assert reply.usage["input_tokens"] == 50


@pytest.mark.parametrize(("status", "error", "retryable"), [
    (401, ProviderAuthError, False),
    (402, ProviderAuthError, False),
    (429, ProviderError, True),
    (503, ProviderError, True),
    (400, ProviderError, False),
])
def test_http_errors_are_classified(status, error, retryable):
    provider = OpenAICompatibleProvider("x", "https://llm.test/v1", "k", "m",
                                        client=mock_client(lambda r: httpx.Response(status)))
    with pytest.raises(error) as info:
        provider.complete("p")
    assert info.value.retryable is retryable


def test_network_errors_are_retryable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    provider = GeminiProvider("k", "m", client=mock_client(handler))
    with pytest.raises(ProviderError) as info:
        provider.complete("p")
    assert info.value.retryable


def test_build_providers_respects_order_and_skips_missing_keys():
    settings = Settings(_env_file=None, AI_PROVIDERS="openrouter, gemini, openai_compatible",
                        GEMINI_API_KEY="g", OPENROUTER_API_KEY="o", OPENAI_COMPATIBLE_API_KEY="")
    assert [p.name for p in build_providers(settings)] == ["openrouter", "gemini"]


# ── Validation service ───────────────────────────────────────────────────


class FakeProvider:
    def __init__(self, name: str, outcomes: list):
        self.name, self.model, self.outcomes, self.calls = name, "fake", list(outcomes), 0

    def complete(self, prompt: str, max_tokens: int = 4096) -> LLMReply:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return LLMReply(outcome, {"provider": self.name, "input_tokens": 1, "output_tokens": 1})


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
    (tmp_path / "vite.config.js").write_text("export default { base: '/' }\n")
    (tmp_path / "src" / "main.jsx").write_text("import App from './App'\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("ignored")
    return tmp_path


def service(*providers, settings=None) -> AIValidationService:
    return AIValidationService(settings=settings or Settings(_env_file=None),
                               providers=list(providers), sleep=lambda _: None)


FIX = json.dumps({
    "status": "needs_fixes",
    "issues": [{"file": "vite.config.js", "line": 1, "severity": "critical", "description": "base"}],
    "fixes": [{"file": "vite.config.js", "description": "set base",
               "original": "base: '/'", "replacement": "base: '/shop/'"}],
    "summary": "Base path fixed.",
})


def test_fix_is_applied_and_usage_attached(project: Path):
    primary = FakeProvider("gemini", [FIX])
    result = service(primary).validate_and_fix(str(project), "shop", DeploymentMode.DEMO)
    assert result.status == "needs_fixes"
    assert "base: '/shop/'" in (project / "vite.config.js").read_text()
    assert result.token_usage["provider"] == "gemini"


def test_prompt_targets_the_demo_base_path_and_skips_node_modules(project: Path):
    captured = {}

    class Capture(FakeProvider):
        def complete(self, prompt, max_tokens=4096):
            captured["prompt"] = prompt
            return super().complete(prompt, max_tokens)

    service(Capture("x", ['{"status": "pass"}'])).validate_and_fix(
        str(project), "shop", DeploymentMode.DEMO, has_router=True)
    assert 'base path: "/shop/"' in captured["prompt"]
    assert "ROUTER BASENAME" in captured["prompt"]
    assert "node_modules" not in captured["prompt"]


def test_retries_transient_errors_then_succeeds(project: Path):
    flaky = FakeProvider("gemini", [ProviderError("429", retryable=True), '{"status": "pass"}'])
    result = service(flaky).validate_and_fix(str(project), "shop", DeploymentMode.PROD)
    assert flaky.calls == 2
    assert result.status == "pass"


def test_falls_back_to_next_provider_on_auth_error(project: Path):
    broken = FakeProvider("gemini", [ProviderAuthError("bad key", 401)])
    backup = FakeProvider("openrouter", ['{"status": "pass", "summary": "ok"}'])
    result = service(broken, backup).validate_and_fix(str(project), "shop", DeploymentMode.PROD)
    assert broken.calls == 1  # no retry on an invalid key
    assert result.summary == "ok"
    assert result.token_usage["provider"] == "openrouter"


def test_pipeline_continues_when_no_provider_answers(project: Path):
    down = FakeProvider("gemini", [ProviderError("503", retryable=True)] * 3)
    result = service(down).validate_and_fix(str(project), "shop", DeploymentMode.PROD)
    assert down.calls == 3
    assert result.status == "pass"
    assert "skipped" in result.summary
    assert service().validate_and_fix(str(project), "shop", DeploymentMode.PROD).status == "pass"


def test_unparseable_response_does_not_block(project: Path):
    result = service(FakeProvider("x", ["```json\nnot json\n```"])).validate_and_fix(
        str(project), "shop", DeploymentMode.PROD)
    assert result.status == "pass"
    assert "could not be parsed" in result.summary


def test_fixes_cannot_escape_the_source_directory(tmp_path: Path):
    source = tmp_path / "site"
    sibling = tmp_path / "site-evil"  # shares the "site" prefix: startswith() would accept it
    source.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("keep")
    (source / "index.html").write_text("<html></html>")
    fixes = [
        {"file": "../site-evil/secret.txt", "original": "keep", "replacement": "pwned"},
        {"file": "../../etc/passwd", "original": "root", "replacement": "x"},
        {"file": "missing.js", "original": "a", "replacement": "b"},
        {"file": "index.html", "original": "absent text", "replacement": "b"},
        {"file": "", "original": "", "replacement": ""},
    ]
    applied = service()._apply_fixes(str(source), fixes)
    assert applied == 0
    assert (sibling / "secret.txt").read_text() == "keep"
