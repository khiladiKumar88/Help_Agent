"""Token-bucket rate limiter + daily call budget, matched to the provider's free tier.

Two independent guards:
  * a per-minute token bucket (RPM, optionally TPM) — waiting is fine, it refills;
  * a daily call budget (RPD) — when it is gone the agent PAUSES for the day. It never
    trades without an LLM decision, so exhaustion raises LLMBudgetExhausted.

Two different time sources, on purpose:
  * the bucket and its waits use the REAL monotonic clock, because the provider's rate limit is
    a wall-clock limit no matter what the simulated clock says. Using the injected Clock would
    also deadlock a replay: `ReplayClock.sleep` only returns when the replay driver advances
    time, and the driver is itself blocked awaiting the signal handler that is waiting here.
  * the daily budget's day key uses the injected Clock, so replays and tests are deterministic.

The daily counter persists through the journal's key/value state, so a restart cannot hand the
agent a fresh budget.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from papermind.core.clock import Clock
from papermind.core.config import LLMRateLimits
from papermind.llm.base import LLMBudgetExhausted, LLMRateLimited


class BudgetStore(Protocol):
    """Just enough of the Journal's state API to persist the daily counter."""

    def get_state(self, key: str) -> Any: ...
    def set_state(self, key: str, value: Any) -> None: ...


STATE_KEY = "llm_budget"


def _parse_ts(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        return None


@dataclass
class Usage:
    day: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0
    cached_hits: int = 0
    # set when the PROVIDER says its own cap will not lift for a while (see `park`)
    parked_until: datetime | None = None
    parked_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "errors": self.errors,
            "cached_hits": self.cached_hits,
            "parked_until": self.parked_until.isoformat() if self.parked_until else None,
            "parked_reason": self.parked_reason,
        }


@dataclass
class _Bucket:
    """Token bucket: `capacity` tokens, refilled at `rate` per second.

    `peek` and `reserve` are deliberately separate so a caller can be told its wait, decide
    whether the wait is acceptable, and only then book its slot. A reservation may push `tokens`
    negative: the deficit divided by the rate is exactly how long THAT caller must wait, which
    is what lets several callers wait concurrently and still arrive within the limit, each on
    its own staggered slot, instead of queueing behind one another.
    """

    capacity: float
    rate: float
    tokens: float = field(init=False)
    updated: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def peek(self, now: float, n: float = 1.0) -> float:
        """Refill, then return the seconds to wait for n tokens. Consumes nothing."""
        if self.updated is None:
            self.updated = now
        else:
            elapsed = max(0.0, now - self.updated)
            self.updated = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= n:
            return 0.0
        if self.rate <= 0:
            return float("inf")
        return (n - self.tokens) / self.rate

    def reserve(self, n: float = 1.0) -> None:
        """Book n tokens, going negative if they are not there yet. Call `peek` first."""
        self.tokens -= n


class RateLimiter:
    """Guards one provider. `acquire()` waits for the minute bucket and enforces the daily cap."""

    def __init__(
        self,
        limits: LLMRateLimits,
        clock: Clock,
        store: BudgetStore | None = None,
        max_wait_seconds: float = 20.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.limits = limits
        self.clock = clock
        self.store = store
        self.max_wait_seconds = max_wait_seconds
        self._monotonic = monotonic
        self._sleep = sleep or asyncio.sleep
        cap = float(limits.burst or limits.requests_per_minute)
        self._requests = _Bucket(capacity=cap, rate=limits.requests_per_minute / 60.0)
        self._tokens = (
            _Bucket(capacity=float(limits.tokens_per_minute), rate=limits.tokens_per_minute / 60.0)
            if limits.tokens_per_minute
            else None
        )
        self._lock = asyncio.Lock()
        self._usage = self._load_usage()

    # ------------------------------------------------------------------ budget
    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _load_usage(self) -> Usage:
        today = self._today()
        raw = self.store.get_state(STATE_KEY) if self.store else None
        if isinstance(raw, dict) and raw.get("day") == today:
            return Usage(
                day=today,
                calls=int(raw.get("calls", 0)),
                input_tokens=int(raw.get("input_tokens", 0)),
                output_tokens=int(raw.get("output_tokens", 0)),
                errors=int(raw.get("errors", 0)),
                cached_hits=int(raw.get("cached_hits", 0)),
                parked_until=_parse_ts(raw.get("parked_until")),
                parked_reason=raw.get("parked_reason") or None,
            )
        return Usage(day=today)

    def _roll_day(self) -> None:
        if self._usage.day != self._today():
            self._usage = Usage(day=self._today())
            self._persist()

    def _persist(self) -> None:
        if self.store is not None:
            self.store.set_state(STATE_KEY, self._usage.as_dict())

    @property
    def usage(self) -> Usage:
        self._roll_day()
        return self._usage

    def remaining(self) -> int:
        return max(0, self.limits.requests_per_day - self.usage.calls)

    def park(self, seconds: float, reason: str) -> None:
        """The provider's OWN cap will not lift for `seconds`: stop asking until it does.

        Our daily budget is only a guess at the provider's; the provider is the authority. When
        it answers a 429 with a retry-after measured in hours, every further signal would spend
        a full timeout learning the same thing, so the agent parks and says why. The deadline is
        persisted, so a restart cannot talk us back into hammering a closed door.
        """
        until = self.clock.now() + timedelta(seconds=max(0.0, seconds))
        current = self._usage.parked_until
        if current is not None and current >= until:
            return
        self._usage.parked_until = until
        self._usage.parked_reason = reason
        self._persist()

    def parked_until(self) -> datetime | None:
        """The park deadline, or None when the limiter is free to call."""
        until = self.usage.parked_until
        return until if until is not None and self.clock.now() < until else None

    def budget_exhausted(self) -> bool:
        return self.remaining() <= 0 or self.parked_until() is not None

    # ------------------------------------------------------------------ gate
    async def acquire(self, est_input_tokens: int = 0) -> float:
        """Book one call and wait for its slot. Returns the seconds spent waiting.

        Raises instead of letting the agent proceed without a decision.

        The slot is booked under the lock but WAITED FOR OUTSIDE it. Holding the lock across the
        sleep would make every concurrent caller queue behind the one ahead of it, so two books
        reacting to the same candle close would run strictly one after the other even when the
        per-minute budget had room for both. The returned wait is the caller's own queue time;
        it is deliberately not part of the provider's per-call timeout.
        """
        async with self._lock:
            self._roll_day()
            parked = self.parked_until()
            if parked is not None:
                raise LLMBudgetExhausted(
                    f"{self._usage.parked_reason or 'provider rate limit'} — not calling again "
                    f"until {parked.isoformat(timespec='seconds')}"
                )
            if self.budget_exhausted():
                raise LLMBudgetExhausted(
                    f"daily LLM budget used up ({self._usage.calls}/{self.limits.requests_per_day} calls today)"
                )
            now = self._monotonic()
            wait = self._requests.peek(now)
            if self._tokens is not None and est_input_tokens:
                wait = max(wait, self._tokens.peek(now, float(est_input_tokens)))
            if wait > self.max_wait_seconds:
                # nothing was booked, so a rejected caller does not steal a later caller's slot
                raise LLMRateLimited(
                    f"rate limit: would need to wait {wait:.1f}s (max {self.max_wait_seconds:.0f}s)", retry_after=wait
                )
            self._requests.reserve()
            if self._tokens is not None and est_input_tokens:
                self._tokens.reserve(float(est_input_tokens))
            # counted at reservation time: a call we fired but lost the answer to still used quota
            self._usage.calls += 1
            self._persist()
        if wait > 0:
            await self._sleep(wait)
        return wait

    def record_result(self, input_tokens: int | None, output_tokens: int | None, error: bool = False) -> None:
        self._usage.input_tokens += int(input_tokens or 0)
        self._usage.output_tokens += int(output_tokens or 0)
        if error:
            self._usage.errors += 1
        self._persist()

    def record_cache_hit(self) -> None:
        """A cached answer costs no quota, but we still want to see it in the UI."""
        self._roll_day()
        self._usage.cached_hits += 1
        self._persist()

    def snapshot(self) -> dict[str, Any]:
        u = self.usage
        parked = self.parked_until()
        return {
            **u.as_dict(),
            "parked_until": parked.isoformat() if parked else None,
            "parked_reason": u.parked_reason if parked else None,
            "daily_budget": self.limits.requests_per_day,
            "remaining": self.remaining(),
            "requests_per_minute": self.limits.requests_per_minute,
            "tokens_per_minute": self.limits.tokens_per_minute,
            "exhausted": self.budget_exhausted(),
        }
