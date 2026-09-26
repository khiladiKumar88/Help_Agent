"""Shared fixtures. Every test is deterministic: ReplayClock + in-memory SQLite + fixtures.
No test touches a live API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from papermind.broker.paper_broker import PaperBroker
from papermind.core.clock import ReplayClock
from papermind.core.config import AppConfig, BookConfig
from papermind.core.events import EventBus, Topic
from papermind.core.types import Actor, Direction, Instrument, OrderRequest, Tick
from papermind.data.market import MarketState
from papermind.db.base import Database
from papermind.instruments.registry import InstrumentRegistry, instrument_from_ccxt_market
from papermind.journal.service import Journal

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC)
BOOK_ID = "crypto-futures-intraday"
BTC = "crypto:binanceusdm:BTC/USDT:USDT"
ETH = "crypto:binanceusdm:ETH/USDT:USDT"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def crypto_charges() -> dict[str, Any]:
    return {
        "kind": "crypto",
        "source_url": "https://example.test",
        "as_of": "2026-01-01",
        "maker_pct": "0.02",
        "taker_pct": "0.05",
    }


def india_charges() -> dict[str, Any]:
    return {
        "kind": "india_fno",
        "source_url": "https://example.test",
        "as_of": "2026-01-01",
        "brokerage_per_order": "20",
        "stt_futures_sell_pct": "0.05",
        "stt_options_sell_pct": "0.15",
        "exchange_futures_pct": "0.00173",
        "exchange_options_pct": "0.03503",
        "sebi_per_crore": "10",
        "stamp_futures_buy_pct": "0.002",
        "stamp_options_buy_pct": "0.003",
        "gst_pct": "18",
    }


def book_dict(**risk: Any) -> dict[str, Any]:
    base_risk: dict[str, Any] = {
        "max_risk_per_trade_pct": "1",
        "max_leverage": "3",
        "max_open_positions": 2,
        "max_trades_per_day": 10,
        "daily_loss_limit_pct": "3",
    }
    base_risk.update(risk)
    return {
        "id": BOOK_ID,
        "market": "crypto",
        "segment": "futures",
        "style": "intraday",
        "currency": "USDT",
        "starting_capital": "1000",
        "data_source": "crypto",
        "instruments": ["BTC/USDT:USDT", "ETH/USDT:USDT"],
        "charges": "crypto_test",
        "slippage": {"model": "bps", "bps": "2", "est_spread_bps": "2"},
        "risk": base_risk,
    }


def make_cfg(book: dict[str, Any] | None = None, **extra: Any) -> AppConfig:
    b = book or book_dict()
    raw: dict[str, Any] = {
        "db_url": "sqlite://",
        "books": {b["id"]: b},
        "charges": {"crypto_test": crypto_charges(), "india_test": india_charges()},
    }
    raw.update(extra)
    return AppConfig.model_validate(raw)


def ccxt_instruments() -> list[Instrument]:
    markets = load_fixture("binanceusdm_markets.json")
    out = []
    for sym in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
        inst = instrument_from_ccxt_market("binanceusdm", markets[sym], 4)
        assert inst is not None
        out.append(inst)
    return out


@dataclass
class Env:
    cfg: AppConfig
    clock: ReplayClock
    bus: EventBus
    db: Database
    journal: Journal
    registry: InstrumentRegistry
    market: MarketState
    broker: PaperBroker
    events: list[tuple[str, Any]]

    @property
    def book(self) -> BookConfig:
        return self.cfg.book(BOOK_ID)

    async def tick(
        self, ltp: str | Decimal, bid: str | None = None, ask: str | None = None, inst: str = BTC, advance: float = 1
    ) -> Tick:
        self.clock.advance(advance)
        t = Tick(
            instrument_id=inst,
            ts=self.clock.now(),
            ltp=Decimal(ltp),
            bid=Decimal(bid) if bid else None,
            ask=Decimal(ask) if ask else None,
            source="test",
        )
        self.market.update(t, self.clock.now())
        await self.broker.on_tick(t)
        return t

    def req(
        self,
        direction: Direction = Direction.LONG,
        qty: str = "0.005",
        sl: str | None = "64000",
        target: str | None = None,
        inst: str = BTC,
        **kw: Any,
    ) -> OrderRequest:
        return OrderRequest(
            book_id=BOOK_ID,
            actor=kw.pop("actor", Actor.HUMAN),
            instrument_id=inst,
            direction=direction,
            qty=Decimal(qty),
            stop_loss=Decimal(sl) if sl else None,
            target=Decimal(target) if target else None,
            **kw,
        )


def build_env(cfg: AppConfig | None = None, db: Database | None = None, clock: ReplayClock | None = None) -> Env:
    cfg = cfg or make_cfg()
    clock = clock or ReplayClock(T0)
    db = db or Database("sqlite://")
    db.create_all()
    bus = EventBus()
    events: list[tuple[str, Any]] = []
    for topic in Topic:
        bus.subscribe(topic, lambda p, _t=topic: events.append((str(_t), p)))
    journal = Journal(db, clock)
    registry = InstrumentRegistry(db)
    registry.upsert(ccxt_instruments(), clock.now())
    market = MarketState()
    broker = PaperBroker(cfg, clock, bus, journal, registry, market)
    broker.start()
    return Env(cfg, clock, bus, db, journal, registry, market, broker, events)


@pytest.fixture
def env() -> Env:
    return build_env()
