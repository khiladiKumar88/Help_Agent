"""Token bucket + daily call budget. Deterministic: the monotonic clock and sleep are injected."""

from __future__ import annotations

from typing import Any

import pytest

from papermind.core.clock import ReplayClock
from papermind.core.config import LLMRateLimits
from papermind.llm.base import LLMBudgetExhausted, LLMRateLimited
from papermind.llm.limiter import STATE_KEY, RateLimiter
from tests.conftest import T0


class FakeStore:
    """Stands in for the Journal's key/value state."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(initial or {})

    def get_state(self, key: str) -> Any:
        return self.data.get(key)

    def set_state(self, key: str, value: Any) -> None:
        self.data[key] = value


class Ticker:
    """Controllable monotonic clock plus a sleep that advances it instead of waiting."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make(limits: LLMRateLimits | None = None, store: FakeStore | None = None, clock: ReplayClock | None = None):
    tick = Ticker()
    lim = RateLimiter(
        limits or LLMRateLimits(requests_per_minute=60, requests_per_day=5, tokens_per_minute=None),
        clock or ReplayClock(T0),
        store=store,
        monotonic=tick.monotonic,
        sleep=tick.sleep,
    )
    return lim, tick


async def test_calls_within_the_burst_do_not_wait() -> None:
    lim, tick = make(LLMRateLimits(requests_per_minute=60, burst=3, requests_per_day=100, tokens_per_minute=None))
    for _ in range(3):
        await lim.acquire()
    assert tick.slept == []  # the bucket started full
    assert lim.usage.calls == 3


async def test_an_empty_bucket_waits_for_a_refill() -> None:
    # 60/min = 1 per second; a burst of 1 means the second call waits ~1s
    lim, tick = make(LLMRateLimits(requests_per_minute=60, burst=1, requests_per_day=100, tokens_per_minute=None))
    await lim.acquire()
    await lim.acquire()
    assert tick.slept and 0.9 < tick.slept[0] <= 1.0


async def test_a_wait_longer_than_the_cap_is_rate_limited_rather_than_blocking() -> None:
    lim, _ = make(LLMRateLimits(requests_per_minute=1, burst=1, requests_per_day=100, tokens_per_minute=None))
    await lim.acquire()
    with pytest.raises(LLMRateLimited) as exc:
        await lim.acquire()  # would need ~60s
    assert exc.value.retry_after and exc.value.retry_after > 20


async def test_the_daily_budget_is_a_hard_stop() -> None:
    lim, _ = make(LLMRateLimits(requests_per_minute=600, requests_per_day=2, tokens_per_minute=None))
    await lim.acquire()
    await lim.acquire()
    assert lim.remaining() == 0 and lim.budget_exhausted()
    with pytest.raises(LLMBudgetExhausted):
        await lim.acquire()


async def test_the_budget_survives_a_restart() -> None:
    store = FakeStore()
    limits = LLMRateLimits(requests_per_minute=600, requests_per_day=3, tokens_per_minute=None)
    lim, _ = make(limits, store)
    await lim.acquire()
    await lim.acquire()
    assert store.data[STATE_KEY]["calls"] == 2
    # a fresh limiter on the same store must not hand out a fresh budget
    again, _ = make(limits, store)
    assert again.usage.calls == 2 and again.remaining() == 1


async def test_a_new_day_resets_the_budget() -> None:
    store = FakeStore()
    clock = ReplayClock(T0)
    limits = LLMRateLimits(requests_per_minute=600, requests_per_day=1, tokens_per_minute=None)
    lim = RateLimiter(limits, clock, store=store, monotonic=lambda: 0.0)
    await lim.acquire()
    assert lim.budget_exhausted()
    clock.advance(days=1)
    assert not lim.budget_exhausted() and lim.usage.calls == 0


async def test_a_stale_stored_day_is_ignored() -> None:
    store = FakeStore({STATE_KEY: {"day": "1999-01-01", "calls": 99}})
    lim, _ = make(LLMRateLimits(requests_per_minute=600, requests_per_day=5, tokens_per_minute=None), store)
    assert lim.usage.calls == 0


async def test_the_token_bucket_also_throttles() -> None:
    # 600 input tokens/min = 10/s; asking for 60 tokens with an empty bucket waits ~6s
    lim, tick = make(LLMRateLimits(requests_per_minute=600, requests_per_day=100, tokens_per_minute=600))
    await lim.acquire(est_input_tokens=600)  # drains the bucket
    await lim.acquire(est_input_tokens=60)
    assert tick.slept and 5.0 < tick.slept[-1] <= 6.0


def test_cache_hits_are_counted_but_cost_no_quota() -> None:
    lim, _ = make(LLMRateLimits(requests_per_minute=600, requests_per_day=2, tokens_per_minute=None))
    lim.record_cache_hit()
    lim.record_cache_hit()
    assert lim.usage.cached_hits == 2
    assert lim.remaining() == 2 and not lim.budget_exhausted()


def test_snapshot_shows_what_the_ui_needs() -> None:
    lim, _ = make(LLMRateLimits(requests_per_minute=7, requests_per_day=9, tokens_per_minute=None))
    lim.record_result(100, 20)
    snap = lim.snapshot()
    assert snap["daily_budget"] == 9 and snap["remaining"] == 9
    assert snap["requests_per_minute"] == 7 and snap["input_tokens"] == 100
    assert snap["exhausted"] is False


def test_errors_are_counted_separately() -> None:
    lim, _ = make()
    lim.record_result(None, None, error=True)
    assert lim.usage.errors == 1


def test_limits_reject_nonsense() -> None:
    with pytest.raises(ValueError):
        LLMRateLimits(requests_per_minute=0)
    with pytest.raises(ValueError):
        LLMRateLimits(requests_per_day=0)
    with pytest.raises(ValueError):
        LLMRateLimits(burst=0)
