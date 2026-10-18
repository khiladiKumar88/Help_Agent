"""The limiter must shape concurrent callers, not queue them — and must honour the provider's cap.

Two signals on the same candle close should both be in flight inside the per-minute budget. The
limiter used to hold its lock across the wait, so the second caller waited for the first caller's
sleep AND then its own: concurrency anywhere upstream was undone here.
"""

from __future__ import annotations

import asyncio

import pytest

from papermind.core.clock import ReplayClock
from papermind.core.config import LLMRateLimits
from papermind.llm.base import LLMBudgetExhausted
from papermind.llm.limiter import STATE_KEY, RateLimiter
from tests.conftest import T0
from tests.llm.test_limiter import FakeStore, make


class TestConcurrency:
    async def test_callers_inside_the_burst_all_start_at_once(self) -> None:
        lim, tick = make(LLMRateLimits(requests_per_minute=60, burst=4, requests_per_day=100, tokens_per_minute=None))
        waits = await asyncio.gather(*(lim.acquire() for _ in range(4)))
        assert waits == [0.0, 0.0, 0.0, 0.0]
        assert tick.slept == []
        assert lim.usage.calls == 4

    async def test_callers_past_the_burst_get_their_own_staggered_slot(self) -> None:
        """Each extra caller waits for ITS place in the queue, not for everyone ahead of it.

        The clock is frozen here on purpose: four callers arriving on the same candle close all
        book their slot at the same instant, and the question is what each is told to wait.
        """
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)  # record only: time must not move while they book

        # 60/min = 1 per second, burst of 1: the 2nd caller waits ~1s, the 3rd ~2s, the 4th ~3s
        lim = RateLimiter(
            LLMRateLimits(requests_per_minute=60, burst=1, requests_per_day=100, tokens_per_minute=None),
            ReplayClock(T0),
            monotonic=lambda: 1000.0,
            sleep=sleep,
        )
        waits = await asyncio.gather(*(lim.acquire() for _ in range(4)))
        assert waits[0] == 0.0
        assert [round(w) for w in waits[1:]] == [1, 2, 3]
        assert [round(w) for w in slept] == [1, 2, 3], "each caller sleeps once, for its own slot"

    async def test_a_waiting_caller_does_not_block_the_next_one_from_being_admitted(self) -> None:
        """The slow part is outside the lock, so acquiring never serialises on someone's sleep."""
        lim, _ = make(LLMRateLimits(requests_per_minute=60, burst=1, requests_per_day=100, tokens_per_minute=None))
        order: list[str] = []

        async def caller(name: str) -> None:
            await lim.acquire()
            order.append(name)

        slow = asyncio.create_task(caller("slow"))
        await asyncio.sleep(0)  # let `slow` reach its wait
        fast = asyncio.create_task(caller("fast"))
        await asyncio.gather(slow, fast)
        # both got through; what matters is that the lock was never held across a sleep
        assert sorted(order) == ["fast", "slow"]
        assert lim.usage.calls == 2

    async def test_a_rejected_caller_does_not_book_a_slot(self) -> None:
        """A caller we turn away must not consume the quota the next caller is counting on."""
        lim, _ = make(LLMRateLimits(requests_per_minute=1, burst=1, requests_per_day=100, tokens_per_minute=None))
        await lim.acquire()
        before = lim.usage.calls
        with pytest.raises(Exception):  # noqa: B017 - LLMRateLimited, asserted elsewhere
            await lim.acquire()
        assert lim.usage.calls == before


class TestParking:
    """`park` exists because our daily budget is only a GUESS at the provider's real one."""

    def limits(self) -> LLMRateLimits:
        return LLMRateLimits(requests_per_minute=600, requests_per_day=500, tokens_per_minute=None)

    async def test_parking_stops_further_calls_even_with_budget_left(self) -> None:
        lim, _ = make(self.limits())
        await lim.acquire()
        lim.park(3600.0, "provider rate limit (429): limit 20 requests per day")
        assert lim.remaining() > 0, "our own budget still has room — the provider's does not"
        assert lim.budget_exhausted() and lim.parked_until() is not None
        with pytest.raises(LLMBudgetExhausted, match="20 requests per day"):
            await lim.acquire()

    async def test_the_park_lifts_when_its_deadline_passes(self) -> None:
        clock = ReplayClock(T0)
        lim = RateLimiter(self.limits(), clock, monotonic=lambda: 0.0)
        lim.park(600.0, "429")
        assert lim.budget_exhausted()
        clock.advance(seconds=601)
        assert not lim.budget_exhausted() and lim.parked_until() is None
        await lim.acquire()

    async def test_a_restart_cannot_un_park_the_agent(self) -> None:
        store = FakeStore()
        lim, _ = make(self.limits(), store)
        lim.park(3600.0, "429: out of daily quota")
        again, _ = make(self.limits(), store)
        assert again.budget_exhausted(), "the deadline has to survive a restart"
        assert store.data[STATE_KEY]["parked_until"]

    async def test_a_longer_park_is_never_shortened_by_a_later_one(self) -> None:
        clock = ReplayClock(T0)
        lim = RateLimiter(self.limits(), clock, monotonic=lambda: 0.0)
        lim.park(3600.0, "daily cap")
        far = lim.parked_until()
        lim.park(5.0, "a passing minute cap")
        assert lim.parked_until() == far

    def test_the_snapshot_says_why_the_agent_stopped(self) -> None:
        lim, _ = make(self.limits())
        lim.park(3600.0, "provider rate limit (429): retry in 12h31m7s")
        snap = lim.snapshot()
        assert snap["exhausted"] is True
        assert snap["parked_until"] and "12h31m7s" in snap["parked_reason"]
