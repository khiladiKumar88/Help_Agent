"""Staleness watchdog: flags instruments with no tick for > stale_after_seconds.

Stale data => the risk manager refuses new entries (rule R004) and the UI shows a warning.
"""

from __future__ import annotations

import logging

from papermind.core.clock import Clock
from papermind.core.events import EventBus, Topic
from papermind.data.market import MarketState

log = logging.getLogger(__name__)


class StalenessWatchdog:
    def __init__(self, clock: Clock, bus: EventBus, state: MarketState, stale_after_seconds: float) -> None:
        self.clock = clock
        self.bus = bus
        self.state = state
        self.threshold = stale_after_seconds
        self._watched: set[str] = set()
        self._stale: set[str] = set()

    def watch(self, instrument_ids: list[str]) -> None:
        self._watched.update(instrument_ids)

    def is_stale(self, instrument_id: str) -> bool:
        return self.state.is_stale(instrument_id, self.clock.now(), self.threshold)

    def snapshot(self) -> dict[str, dict[str, object]]:
        now = self.clock.now()
        return {
            i: {"stale": self.state.is_stale(i, now, self.threshold), "age_seconds": self.state.age_seconds(i, now)}
            for i in sorted(self._watched)
        }

    async def check(self) -> None:
        now = self.clock.now()
        for inst in sorted(self._watched):
            stale = self.state.is_stale(inst, now, self.threshold)
            age = self.state.age_seconds(inst, now)
            if stale and inst not in self._stale:
                self._stale.add(inst)
                log.warning("market data stale", extra={"instrument_id": inst, "age_seconds": age})
                await self.bus.publish(Topic.DATA_STALE, {"instrument_id": inst, "age_seconds": age})
            elif not stale and inst in self._stale:
                self._stale.discard(inst)
                log.info("market data fresh again", extra={"instrument_id": inst})
                await self.bus.publish(Topic.DATA_FRESH, {"instrument_id": inst, "age_seconds": age})
