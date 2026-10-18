"""Every call's duration is recorded — failures included — and queue time is kept apart from it.

Failures used to store `latency_ms = 0`, so the only calls with timing were the ones that worked.
That is how a day of 60s rate-limit stalls came to look, in the call log, exactly like a day of
instant failures: the one number that would have identified the problem was the one we threw away.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from papermind.llm.base import (
    LLMError,
    LLMProvider,
    LLMRateLimited,
    LLMRequest,
    LLMResponse,
    LLMTimeout,
)
from tests.analyst.conftest import Rig, build_rig, reply


class SlowFailing(LLMProvider):
    """Takes real time and then fails, like a call that stalls until its timeout."""

    name = "slow"

    def __init__(self, error: LLMError, delay: float = 0.05) -> None:
        self.error = error
        self.delay = delay
        self.calls = 0

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        await asyncio.sleep(self.delay)
        raise self.error


class SlowOk(LLMProvider):
    name = "slowok"

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay

    async def complete(self, req: LLMRequest) -> LLMResponse:
        await asyncio.sleep(self.delay)
        return LLMResponse(text=reply(), model=req.model, latency_ms=int(self.delay * 1000))


class TestFailuresRecordTheirDuration:
    async def test_a_timeout_records_how_long_it_actually_took(self) -> None:
        rig = build_rig(SlowFailing(LLMTimeout("no response within 60s")))
        decision = await rig.analyst.decide(rig.signal())
        assert decision.take is False
        call = rig.calls()[0]
        assert call["ok"] is False and call["error_reason"] == "llm_timeout"
        assert call["latency_ms"] > 0, "a stalled call is not an instant one"

    async def test_a_rate_limit_records_its_duration_too(self) -> None:
        rig = build_rig(SlowFailing(LLMRateLimited("provider rate limit (429)")))
        await rig.analyst.decide(rig.signal())
        call = rig.calls()[0]
        assert call["error_reason"] == "llm_rate_limited"
        assert call["latency_ms"] > 0

    async def test_a_successful_call_still_reports_the_providers_own_latency(self) -> None:
        rig = build_rig(SlowOk())
        await rig.analyst.decide(rig.signal())
        call = rig.calls()[0]
        assert call["ok"] is True and call["latency_ms"] > 0


class TestQueueWaitIsNotChargedToTheCall:
    """`llm.timeout_seconds` bounds the provider call, never the wait for a rate-limit slot."""

    async def test_the_wait_is_recorded_separately(self) -> None:
        rig = build_rig(requests_per_minute=60, burst=1)
        waits: list[float] = []
        real_acquire = rig.analyst.limiter.acquire

        async def acquire(est_input_tokens: int = 0) -> float:
            await real_acquire(est_input_tokens=est_input_tokens)
            waits.append(4.0)
            return 4.0  # as if the bucket had made this caller wait 4s for its slot

        rig.analyst.limiter.acquire = acquire  # type: ignore[method-assign]
        await rig.analyst.decide(rig.signal())
        call = rig.calls()[0]
        assert call["queue_wait_ms"] == 4000
        assert call["latency_ms"] < 4000, "the wait is not part of the call's own latency"

    async def test_the_timeout_handed_to_the_provider_ignores_any_queueing(self) -> None:
        """A signal that queued is not given less time to think because of it."""
        seen: list[float] = []

        class Recorder(LLMProvider):
            name = "rec"

            async def complete(self, req: LLMRequest) -> LLMResponse:
                seen.append(req.timeout_seconds)
                return LLMResponse(text=reply(), model=req.model)

        rig = build_rig(Recorder(), timeout_seconds=45.0)

        async def acquire(est_input_tokens: int = 0) -> float:
            return 12.0  # a long queue

        rig.analyst.limiter.acquire = acquire  # type: ignore[method-assign]
        await rig.analyst.decide(rig.signal())
        assert seen == [45.0], "the per-call budget is the configured one, queue or no queue"
        assert rig.calls()[0]["queue_wait_ms"] == 12000

    async def test_a_call_that_never_queued_records_no_wait(self) -> None:
        rig: Rig = build_rig()
        await rig.analyst.decide(rig.signal())
        assert rig.calls()[0]["queue_wait_ms"] == 0


class TestParkingOnTheProvidersOwnCap:
    async def test_a_long_retry_after_pauses_the_analyst(self) -> None:
        """The free tier answers an exhausted daily quota with a retry-after of hours."""
        err = LLMRateLimited("provider rate limit (429): limit 20 requests per day on Free Tier", retry_after=45067.0)
        rig = build_rig(SlowFailing(err, delay=0.0), timeout_seconds=60.0)
        await rig.analyst.decide(rig.signal())
        assert rig.analyst.paused, "asking again before the cap lifts just burns the next signal"
        assert "20 requests per day" in (rig.analyst.last_pause_reason or "")

    async def test_the_next_signal_fails_fast_instead_of_stalling(self) -> None:
        err = LLMRateLimited("429: out of daily quota", retry_after=45067.0)
        provider = SlowFailing(err, delay=0.0)
        rig = build_rig(provider, timeout_seconds=60.0)
        await rig.analyst.decide(rig.signal())
        await rig.analyst.decide(rig.signal())
        assert provider.calls == 1, "the second signal must not reach the provider at all"
        assert rig.calls()[0]["error_reason"] == "llm_budget_exhausted"

    async def test_a_short_retry_after_does_not_pause_anything(self) -> None:
        err = LLMRateLimited("429: slow down", retry_after=2.0)
        rig = build_rig(SlowFailing(err, delay=0.0), timeout_seconds=60.0)
        await rig.analyst.decide(rig.signal())
        assert not rig.analyst.paused

    async def test_the_status_says_why_it_paused(self) -> None:
        err = LLMRateLimited("429: retry in 12h31m7s", retry_after=45067.0)
        rig = build_rig(SlowFailing(err, delay=0.0), timeout_seconds=60.0)
        await rig.analyst.decide(rig.signal())
        status: dict[str, Any] = rig.analyst.status()
        assert status["paused"] is True
        assert "12h31m7s" in str(status["pause_reason"])
        assert status["parked_until"]


@pytest.mark.parametrize("reason", ["llm_timeout", "llm_rate_limited", "llm_error"])
async def test_every_failure_reason_still_means_no_trade(reason: str) -> None:
    errors = {
        "llm_timeout": LLMTimeout("timed out"),
        "llm_rate_limited": LLMRateLimited("429"),
        "llm_error": LLMError("boom"),
    }
    rig = build_rig(SlowFailing(errors[reason], delay=0.0))
    decision = await rig.analyst.decide(rig.signal())
    assert decision.take is False and rig.calls()[0]["error_reason"] == reason
