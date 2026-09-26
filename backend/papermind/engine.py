"""Engine: wires config, clock, DB, market data, risk and the paper broker together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from papermind.broker.paper_broker import PaperBroker
from papermind.core.clock import Clock, RealClock
from papermind.core.config import AppConfig, Settings
from papermind.core.events import EventBus, Topic
from papermind.data.candles import CandleBuilder
from papermind.data.ccxt_provider import CcxtProvider
from papermind.data.market import MarketHub, MarketState
from papermind.data.provider import MarketDataProvider
from papermind.data.simulated import SimulatedProvider
from papermind.data.watchdog import StalenessWatchdog
from papermind.db.base import Database
from papermind.instruments.registry import InstrumentRegistry
from papermind.journal.service import Journal
from papermind.risk.manager import RiskManager
from papermind.risk.session import in_entry_window

log = logging.getLogger(__name__)

ProviderFactory = Callable[["Engine"], MarketDataProvider]


def default_provider_factory(engine: Engine) -> MarketDataProvider:
    crypto = engine.cfg.data.crypto
    if crypto.provider == "simulated":
        return SimulatedProvider(crypto, engine.clock, engine.hub, engine.registry)
    return CcxtProvider(crypto, engine.clock, engine.hub, engine.registry)


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        settings: Settings | None = None,
        clock: Clock | None = None,
        provider_factory: ProviderFactory = default_provider_factory,
        timer_interval: float = 1.0,
    ) -> None:
        self.cfg = cfg
        self.settings = settings or Settings()
        self.clock = clock or RealClock()
        self.bus = EventBus()
        self.db = Database(cfg.db_url)
        self.db.create_all()
        self.journal = Journal(self.db, self.clock)
        self.registry = InstrumentRegistry(self.db)
        self.market = MarketState()
        self.builder = CandleBuilder(close_on_tick_rollover=cfg.data.crypto.provider == "simulated")
        self.hub = MarketHub(self.clock, self.bus, self.market, self.builder, self.db)
        self.watchdog = StalenessWatchdog(self.clock, self.bus, self.market, cfg.data.stale_after_seconds)
        self.broker = PaperBroker(cfg, self.clock, self.bus, self.journal, self.registry, self.market, RiskManager())
        self.provider = provider_factory(self)
        self.timer_interval = timer_interval
        self._timer: asyncio.Task[None] | None = None
        self.started = False
        self.bus.subscribe(Topic.TICK, self.broker.on_tick)
        self.bus.subscribe(Topic.FUNDING, self.broker.on_funding)

    async def start(self) -> None:
        self.registry.load_from_db()
        self.broker.start()
        try:
            await self.provider.start()
        except Exception as exc:  # fail safe: keep API/UI up, data shows as stale/disconnected
            log.exception("market data provider failed to start")
            self.journal.audit("DATA", f"provider failed to start: {exc}", level="ERROR")
        self.watchdog.watch(self.provider.instrument_ids())
        self._timer = asyncio.create_task(self._timer_loop(), name="engine-timer")
        self.started = True
        self.journal.audit("SYSTEM", "engine started", payload={"provider": self.provider.name})

    async def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._timer
        await self.provider.stop()
        self.started = False

    async def _timer_loop(self) -> None:
        while True:
            try:
                await self.watchdog.check()
                await self.broker.on_timer()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("timer loop error")
            await self.clock.sleep(self.timer_interval)

    # ------------------------------------------------------------------ status
    def agent_status(self, book_id: str) -> str:
        """Status pill: running | market_closed | halted | stale | disconnected."""
        if self.broker.kill_switch_engaged:
            return "halted"
        halted, _ = self.broker.halted(book_id)
        if halted:
            return "halted"
        book = self.cfg.book(book_id)
        if book.session is not None and not in_entry_window(book.session, self.clock.now())[0]:
            return "market_closed"
        pstat = self.provider.status()
        if not pstat.get("connected"):
            return "disconnected"
        ids = [i for i in self.provider.instrument_ids() if self.registry.get(i).symbol in book.instruments]
        if ids and all(self.watchdog.is_stale(i) for i in ids):
            return "stale"
        return "running"

    def status(self) -> dict[str, Any]:
        return {
            "server_time": self.clock.now().isoformat(),
            "replay": self.clock.is_replay,
            "kill_switch": self.journal.get_state("kill_switch") or {"engaged": False},
            "provider": self.provider.status(),
            "data": self.watchdog.snapshot(),
            "stale_after_seconds": self.cfg.data.stale_after_seconds,
            "books": {b: {"status": self.agent_status(b), "mode": self.broker.mode(b)} for b in self.cfg.books},
            "llm": {"provider": None, "calls_today": 0, "daily_budget": None, "note": "LLM analyst arrives in Phase 3"},
            "secrets": self.settings.secrets_status(),
        }
