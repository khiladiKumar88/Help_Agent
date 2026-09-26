"""Clocks. Every component reads time from an injected Clock, never datetime.now().

RealClock follows wall time. ReplayClock is advanced explicitly, which makes replays,
backtests and tests deterministic and makes lookahead impossible by construction:
nothing can observe a timestamp the clock has not reached yet.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime not allowed; use timezone-aware UTC")
    return ts.astimezone(UTC)


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Sleep in this clock's timeline."""

    @property
    def is_replay(self) -> bool:
        return False


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class ReplayClock(Clock):
    """Simulated clock. Time only moves when advance()/set() is called."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)
        self._waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    @property
    def is_replay(self) -> bool:
        return True

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        ts = ensure_utc(ts)
        if ts < self._now:
            raise ValueError(f"replay clock cannot go backwards: {ts} < {self._now}")
        self._now = ts
        still: list[tuple[datetime, asyncio.Future[None]]] = []
        for target, fut in self._waiters:
            if fut.done():
                continue
            if target <= ts:
                fut.set_result(None)
            else:
                still.append((target, fut))
        self._waiters = still

    def advance(self, seconds: float = 0, **kwargs: float) -> datetime:
        self.set(self._now + timedelta(seconds=seconds, **kwargs))
        return self._now

    async def sleep(self, seconds: float) -> None:
        target = self._now + timedelta(seconds=max(0.0, seconds))
        if target <= self._now:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((target, fut))
        await fut
