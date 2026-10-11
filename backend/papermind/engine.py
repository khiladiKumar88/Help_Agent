"""Engine: wires config, clock, DB, market data, scanner, agent, risk and the paper broker together.

The SAME Engine class runs live (RealClock + live provider) and in replays/backtests
(ReplayClock + HistoricalReplayProvider). Only the clock, provider and options differ.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from papermind.analyst.analyst import LLMAnalyst
from papermind.analyst.factory import AnalystUnavailable, build_analyst
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
from papermind.scanner.agent import AgentExecutor, BaselineAgent, Decider
from papermind.scanner.scanner import FundingFn, Scanner
from papermind.strategies.base import Strategy

log = logging.getLogger(__name__)

ProviderFactory = Callable[["Engine"], MarketDataProvider]


def default_provider_factory(engine: Engine) -> MarketDataProvider:
    crypto = engine.cfg.data.crypto
    if crypto.provider == "simulated":
        return SimulatedProvider(crypto, engine.clock, engine.hub, engine.registry)
    return CcxtProvider(crypto, engine.clock, engine.hub, engine.registry)


@dataclass
class EngineOptions:
    drive_timers: bool = True  # False in replays: the replay provider steps the timers itself
    force_execute: bool = False  # agent trades regardless of book mode (backtests / replays)
    trading_starts_at: datetime | None = None  # no signals before this (backtest warm-up)
    base_tf: str = "1m"
    persist_candles: bool = True
    funding_fn: FundingFn | None = None
    strategies: dict[str, list[Strategy]] | None = None
    decider: Decider | None = None
    # Replays/backtests pass their own provider (FakeProvider, or the real one with the response
    # cache) so a historical run never silently burns live quota.
    llm_provider: Any | None = None
    build_llm: bool = True  # False => rules decider only (the explicit "LLM off" mode)


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        settings: Settings | None = None,
        clock: Clock | None = None,
        provider_factory: ProviderFactory = default_provider_factory,
        timer_interval: float = 1.0,
        options: EngineOptions | None = None,
    ) -> None:
        self.options = opts = options or EngineOptions()
        self.cfg = cfg
        self.settings = settings or Settings()
        self.clock = clock or RealClock()
        self.bus = EventBus()
        self.db = Database(cfg.db_url)
        self.db.init_schema()
        self.journal = Journal(self.db, self.clock)
        self.registry = InstrumentRegistry(self.db)
        self.market = MarketState()
        self.builder = CandleBuilder(
            close_on_tick_rollover=cfg.data.crypto.provider == "simulated" and opts.drive_timers,
            base_tf=opts.base_tf,
        )
        self.hub = MarketHub(self.clock, self.bus, self.market, self.builder, self.db if opts.persist_candles else None)
        self.watchdog = StalenessWatchdog(self.clock, self.bus, self.market, cfg.data.stale_after_seconds)
        self.broker = PaperBroker(cfg, self.clock, self.bus, self.journal, self.registry, self.market, RiskManager())
        self.provider = provider_factory(self)
        self.timer_interval = timer_interval
        self._timer: asyncio.Task[None] | None = None
        self.started = False
        self.bus.subscribe(Topic.TICK, self.broker.on_tick)
        self.bus.subscribe(Topic.FUNDING, self.broker.on_funding)
        self.scanner = Scanner(
            cfg,
            self.clock,
            self.bus,
            self.registry,
            self.builder,
            self.journal,
            funding_fn=opts.funding_fn or self._live_funding,
            strategies=opts.strategies,
            trading_starts_at=opts.trading_starts_at,
        )
        self.analyst: LLMAnalyst | None = None
        self.llm_error: str | None = None
        decider = opts.decider
        if decider is None and opts.build_llm:
            try:
                self.analyst = build_analyst(
                    cfg,
                    self.clock,
                    self.journal,
                    self.registry,
                    provider=opts.llm_provider,
                    settings=self.settings,  # so a key in .env is actually found
                )
            except AnalystUnavailable as exc:
                # No silent fallback: without an analyst the agent stays out of Auto and the UI
                # says why. Trading every signal instead would be exactly the wrong default.
                self.llm_error = str(exc)
                log.warning("LLM analyst unavailable: %s", exc)
                self.journal.audit("LLM", f"analyst unavailable: {exc}", level="WARNING")
            decider = self.analyst
        self.executor = AgentExecutor(
            self.bus,
            self.broker,
            self.journal,
            decider,
            opts.force_execute,
            clock=self.clock,
            shadow_raw=self.analyst is not None,
        )
        self.baseline = BaselineAgent(self.bus, self.broker, self.journal, self.clock)

    def _live_funding(self, instrument_id: str, _ts: datetime) -> Decimal | None:
        info = self.market.funding.get(instrument_id)
        return info.rate if info else None

    async def start(self) -> None:
        self.registry.load_from_db()
        self.broker.start()
        try:
            await self.provider.start()
        except Exception as exc:  # fail safe: keep API/UI up, data shows as stale/disconnected
            log.exception("market data provider failed to start")
            self.journal.audit("DATA", f"provider failed to start: {exc}", level="ERROR")
        self.watchdog.watch(self.provider.instrument_ids())
        if self.options.drive_timers:
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

    async def run_timers_once(self) -> None:
        await self.watchdog.check()
        await self.broker.on_timer()
        await self.executor.expire_cards()

    async def _timer_loop(self) -> None:
        while True:
            try:
                await self.run_timers_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("timer loop error")
            await self.clock.sleep(self.timer_interval)

    # ------------------------------------------------------------------ status
    def agent_status(self, book_id: str) -> str:
        """Status pill: running | thinking | rate_limited | market_closed | halted | stale | disconnected."""
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
        # the agent needs an LLM verdict to act, so an exhausted budget pauses it
        if self.analyst is not None and self.analyst.paused:
            return "rate_limited"
        return "running"

    def llm_status(self) -> dict[str, Any]:
        """What the top bar shows: provider, model and today's usage against the daily budget."""
        if self.analyst is None:
            return {
                "provider": None,
                "model": self.cfg.llm.model,
                "calls_today": 0,
                "daily_budget": None,
                "enabled": False,
                "note": self.llm_error
                or ("LLM is switched off (llm.provider: off) — the rules agent takes every signal"),
            }
        st = self.analyst.status()
        return {**st, "calls_today": st["calls"], "enabled": True, "note": None}

    def status(self) -> dict[str, Any]:
        return {
            "server_time": self.clock.now().isoformat(),
            "replay": self.clock.is_replay,
            "kill_switch": self.journal.get_state("kill_switch") or {"engaged": False},
            "provider": self.provider.status(),
            "data": self.watchdog.snapshot(),
            "stale_after_seconds": self.cfg.data.stale_after_seconds,
            "books": {b: {"status": self.agent_status(b), "mode": self.broker.mode(b)} for b in self.cfg.books},
            "llm": self.llm_status(),
            "secrets": self.settings.secrets_status(),
            "scanner": {"signals": self.scanner.signals_emitted, "errors": self.scanner.errors},
        }
