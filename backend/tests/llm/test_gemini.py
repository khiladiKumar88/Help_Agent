"""GeminiProvider: request shape and error mapping. The SDK client is stubbed — no network.

The point of these tests is that every provider failure becomes the right LLMError subclass, so
the analyst's fail-safe path can distinguish "retry later" from "give up".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from google.genai import errors as genai_errors

from papermind.core.config import LLMConfig
from papermind.llm.base import (
    LLMError,
    LLMInvalidOutput,
    LLMNotConfigured,
    LLMRateLimited,
    LLMRequest,
    LLMTimeout,
)
from papermind.llm.gemini import GeminiProvider


def cfg(**over: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "provider": "gemini",
        "model": "gemini-3.8-flash",
        "limits": {"max_retries": 1, "backoff_base_seconds": 0.0, "backoff_max_seconds": 0.0},
    }
    base.update(over)
    return LLMConfig.model_validate(base)


def request(timeout: float = 5.0) -> LLMRequest:
    return LLMRequest(
        system="be careful",
        prompt="judge this signal",
        schema={"type": "object"},
        model="gemini-3.8-flash",
        temperature=0.2,
        max_output_tokens=512,
        thinking_level="low",
        timeout_seconds=timeout,
    )


class StubUsage:
    total_input_tokens = 123
    total_output_tokens = 45
    total_thought_tokens = 6
    total_tokens = 174


class StubInteraction:
    def __init__(self, text: str = '{"ok": true}', status: str = "completed", errors: Any = None) -> None:
        self.output_text = text
        self.status = status
        self.errors = errors
        self.usage = StubUsage()
        self.model = "gemini-3.8-flash"
        self.id = "int_1"


class StubInteractions:
    """Records the kwargs the provider sent, and returns/raises what the test scripted."""

    def __init__(self, result: Any = None, raises: BaseException | None = None) -> None:
        self.result = result if result is not None else StubInteraction()
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if callable(self.result):
            return self.result()
        return self.result


def provider(interactions: StubInteractions, conf: LLMConfig | None = None) -> GeminiProvider:
    """Inject a stub client so no real SDK client (or network) is involved."""
    client = type("C", (), {"aio": type("A", (), {"interactions": interactions})()})()
    return GeminiProvider(conf or cfg(), client=client)


def api_error(code: int, message: str = "boom") -> genai_errors.APIError:
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, {"error": {"message": message, "status": "ERR"}})


class TestConstruction:
    def test_a_missing_api_key_is_a_clear_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(LLMNotConfigured, match="GEMINI_API_KEY"):
            GeminiProvider(cfg())

    def test_a_blank_api_key_counts_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "   ")
        with pytest.raises(LLMNotConfigured):
            GeminiProvider(cfg())

    def test_the_key_env_var_name_comes_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_OWN_KEY", "abc")
        p = GeminiProvider(cfg(api_key_env="MY_OWN_KEY"))
        assert p.name == "gemini"


class TestRequestShape:
    async def test_the_model_and_config_come_from_the_request(self) -> None:
        stub = StubInteractions()
        resp = await provider(stub).complete(request())
        sent = stub.calls[0]
        assert sent["model"] == "gemini-3.8-flash"
        assert sent["input"] == "judge this signal"
        assert sent["system_instruction"] == "be careful"
        assert sent["generation_config"]["temperature"] == 0.2
        assert sent["generation_config"]["max_output_tokens"] == 512
        assert sent["generation_config"]["thinking_level"] == "low"
        assert resp.text == '{"ok": true}'

    async def test_json_output_is_requested_with_the_schema(self) -> None:
        stub = StubInteractions()
        await provider(stub).complete(request())
        fmt = stub.calls[0]["response_format"]
        assert fmt["mime_type"] == "application/json"
        assert fmt["schema"] == {"type": "object"}

    async def test_token_usage_is_reported(self) -> None:
        resp = await provider(StubInteractions()).complete(request())
        assert resp.input_tokens == 123 and resp.output_tokens == 45
        assert resp.thought_tokens == 6 and resp.total_tokens == 174
        assert resp.latency_ms >= 0 and resp.cached is False

    async def test_the_api_key_is_never_part_of_the_request_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key")
        stub = StubInteractions()
        await provider(stub).complete(request())
        assert "super-secret-key" not in repr(stub.calls[0])


class TestErrorMapping:
    async def test_429_becomes_rate_limited(self) -> None:
        with pytest.raises(LLMRateLimited):
            await provider(StubInteractions(raises=api_error(429))).complete(request())

    async def test_a_timeout_becomes_llm_timeout(self) -> None:
        with pytest.raises(LLMTimeout, match="within"):
            await provider(StubInteractions(raises=TimeoutError())).complete(request())

    async def test_a_slow_response_times_out(self) -> None:
        class Slow(StubInteractions):
            async def create(self, **kwargs: Any) -> Any:
                await asyncio.sleep(1.0)
                return StubInteraction()

        with pytest.raises(LLMTimeout):
            await provider(Slow()).complete(request(timeout=0.01))

    async def test_a_400_fails_fast_without_retrying(self) -> None:
        stub = StubInteractions(raises=api_error(400, "bad request"))
        with pytest.raises(LLMError, match="400"):
            await provider(stub).complete(request())
        assert len(stub.calls) == 1, "a client error is not worth retrying"

    async def test_a_500_is_retried_then_gives_up(self) -> None:
        stub = StubInteractions(raises=api_error(503, "unavailable"))
        with pytest.raises(LLMError):
            await provider(stub).complete(request())
        assert len(stub.calls) == 2, "max_retries=1 means two attempts"

    async def test_a_transport_failure_is_retried(self) -> None:
        stub = StubInteractions(raises=ConnectionResetError("socket"))
        with pytest.raises(LLMError):
            await provider(stub).complete(request())
        assert len(stub.calls) == 2

    async def test_a_retry_can_succeed(self) -> None:
        calls = {"n": 0}

        class Flaky(StubInteractions):
            async def create(self, **kwargs: Any) -> Any:
                self.calls.append(kwargs)
                calls["n"] += 1
                if calls["n"] == 1:
                    raise api_error(503)
                return StubInteraction('{"second": "try"}')

        resp = await provider(Flaky()).complete(request())
        assert resp.text == '{"second": "try"}' and calls["n"] == 2

    async def test_an_empty_answer_is_invalid_output(self) -> None:
        with pytest.raises(LLMInvalidOutput, match="empty"):
            await provider(StubInteractions(result=StubInteraction(text=""))).complete(request())

    async def test_a_failed_interaction_is_invalid_output(self) -> None:
        class Err:
            code = "SAFETY"
            message = "blocked"

        stub = StubInteractions(result=StubInteraction(status="failed", errors=[Err()]))
        with pytest.raises(LLMInvalidOutput, match="failed"):
            await provider(stub).complete(request())

    async def test_an_incomplete_interaction_is_invalid_output(self) -> None:
        stub = StubInteractions(result=StubInteraction(status="incomplete"))
        with pytest.raises(LLMInvalidOutput):
            await provider(stub).complete(request())

    async def test_retries_are_bounded_by_config(self) -> None:
        conf = cfg(limits={"max_retries": 3, "backoff_base_seconds": 0.0, "backoff_max_seconds": 0.0})
        stub = StubInteractions(raises=api_error(500))
        with pytest.raises(LLMError):
            await provider(stub, conf).complete(request())
        assert len(stub.calls) == 4
