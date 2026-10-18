"""The Interactions API's OWN error classes must be classified correctly.

`client.aio.interactions.create` does not raise `google.genai.errors.APIError`; it raises its own
hierarchy, which carries `status_code` and an httpx `response` instead of `code`. Matching on the
class meant every one of those fell through to the generic transport branch: a 429 was retried as
if it were a dropped connection and surfaced as a timeout, so a whole day of rate limiting was
recorded as `llm_timeout` and the real reason never reached the call log.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from papermind.llm.base import LLMError, LLMRateLimited, LLMRequest
from papermind.llm.gemini import GeminiProvider
from tests.llm.test_gemini import StubInteractions, cfg, provider, request


class InteractionsAPIError(Exception):
    """Shaped like `google.genai._gaos.lib.compat_errors.APIStatusError`."""

    def __init__(self, status_code: int, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = httpx.Response(
            status_code, headers=headers or {}, request=httpx.Request("POST", "https://example.invalid/v1beta")
        )


DAILY_CAP = (
    "Rate limit exceeded for model gemini-3.8-flash (limit: 20 requests per day on Free Tier). "
    "Please retry in 12h31m7s or upgrade your tier."
)


class TestInteractionsErrors:
    async def test_a_429_is_a_rate_limit_not_a_transport_error(self) -> None:
        stub = StubInteractions(raises=InteractionsAPIError(429, DAILY_CAP))
        with pytest.raises(LLMRateLimited) as exc:
            await provider(stub).complete(request())
        assert "20 requests per day" in str(exc.value)

    async def test_the_providers_retry_after_is_carried_through(self) -> None:
        stub = StubInteractions(raises=InteractionsAPIError(429, DAILY_CAP, {"retry-after": "45067"}))
        with pytest.raises(LLMRateLimited) as exc:
            await provider(stub).complete(request())
        assert exc.value.retry_after == 45067.0

    async def test_a_cap_we_cannot_outwait_is_not_retried(self) -> None:
        """Sleeping four times on a cap that lifts in twelve hours only burns the decision."""
        conf = cfg(limits={"max_retries": 3, "backoff_base_seconds": 0.0, "backoff_max_seconds": 1.0})
        stub = StubInteractions(raises=InteractionsAPIError(429, DAILY_CAP, {"retry-after": "45067"}))
        with pytest.raises(LLMRateLimited):
            await provider(stub, conf).complete(request())
        assert len(stub.calls) == 1

    async def test_a_short_retry_after_is_still_retried(self) -> None:
        conf = cfg(limits={"max_retries": 2, "backoff_base_seconds": 0.0, "backoff_max_seconds": 5.0})
        stub = StubInteractions(raises=InteractionsAPIError(429, "slow down", {"retry-after": "1"}))
        with pytest.raises(LLMRateLimited):
            await provider(stub, conf).complete(request())
        assert len(stub.calls) == 3, "a cap that lifts within the backoff ceiling is worth retrying"

    async def test_a_400_is_fatal_and_never_retried(self) -> None:
        conf = cfg(limits={"max_retries": 3, "backoff_base_seconds": 0.0, "backoff_max_seconds": 0.0})
        stub = StubInteractions(
            raises=InteractionsAPIError(400, "Thinking level THINKING_LEVEL_MINIMAL is not supported")
        )
        with pytest.raises(LLMError, match="400") as exc:
            await provider(stub, conf).complete(request())
        assert not isinstance(exc.value, LLMRateLimited)
        assert len(stub.calls) == 1, "a rejected request cannot succeed on a second try"

    async def test_a_503_is_still_retried(self) -> None:
        conf = cfg(limits={"max_retries": 2, "backoff_base_seconds": 0.0, "backoff_max_seconds": 0.0})
        stub = StubInteractions(raises=InteractionsAPIError(503, "overloaded"))
        with pytest.raises(LLMError):
            await provider(stub, conf).complete(request())
        assert len(stub.calls) == 3


class TestTransportSettings:
    """The SDK builds its httpx client with `timeout=None` at every layer, pool included."""

    def test_the_client_is_given_explicit_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class FakeGenaiClient:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        from google import genai

        monkeypatch.setattr(genai, "Client", FakeGenaiClient)
        GeminiProvider(cfg(timeout_seconds=45.0), api_key="k")

        args = captured["http_options"].async_client_args
        timeout = args["timeout"]
        assert timeout.read == 45.0, "read has to cover the model's own latency"
        for phase in (timeout.connect, timeout.write, timeout.pool):
            assert phase is not None and 0 < phase <= 20.0, "a stalled socket or pool must not wait forever"

    def test_the_sdk_may_never_retry_a_429_itself(self) -> None:
        """This is the one that cost a day of signals.

        The SDK sleeps for a 429's Retry-After verbatim and uncapped, and this provider sends
        `retry-after: 45067` when the daily quota is gone — so even ONE permitted retry parks the
        call for 12.5 hours, and the only thing that ends it is our own `wait_for`, which reports
        it as a timeout. The SDK cannot be told to retry nothing (`attempts=0` is rewritten to 1
        internally), so 429 has to be off the list of statuses it may retry. Asserted against the
        SDK's own resolved config, not our input, because the translation in between is where the
        rewrite happens.
        """
        resolved = GeminiProvider(cfg(), api_key="k")._client.aio.interactions.sdk_configuration.retry_config
        codes = resolved.status_codes_override or []
        assert "429" not in codes, "the SDK would sleep on the provider's hours-long retry-after"
        assert codes, "an empty override falls back to the SDK's default list, which includes 429"

    def test_our_own_layer_is_what_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class FakeGenaiClient:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        from google import genai

        monkeypatch.setattr(genai, "Client", FakeGenaiClient)
        GeminiProvider(cfg(), api_key="k")
        codes = captured["http_options"].retry_options.http_status_codes
        assert 429 not in codes and 500 in codes


class TestTimeoutIsPerCall:
    async def test_the_request_carries_the_configured_per_attempt_budget(self) -> None:
        stub = StubInteractions()
        req: LLMRequest = request(timeout=30.0)
        await provider(stub).complete(req)
        # the provider measures and bounds only its own call; queueing is the analyst's business
        assert req.timeout_seconds == 30.0
