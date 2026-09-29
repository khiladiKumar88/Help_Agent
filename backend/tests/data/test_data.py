"""Instrument registry, candle builder, market hub, watchdog, ccxt provider (fake exchange), simulated feed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from papermind.core.clock import ReplayClock
from papermind.core.config import CryptoDataConfig, SimulatedFeedConfig
from papermind.core.events import EventBus, Topic
from papermind.core.types import Candle, InstrumentKind, Tick
from papermind.data.candles import CandleBuilder, bucket_start, tf_seconds
from papermind.data.ccxt_provider import CcxtProvider, ohlcv_to_candles
from papermind.data.market import MarketHub, MarketState
from papermind.data.simulated import SimulatedProvider
from papermind.data.watchdog import StalenessWatchdog
from papermind.db.base import Database
from papermind.db.models import CandleRow
from papermind.instruments.registry import InstrumentRegistry, instrument_from_ccxt_market
from tests.conftest import BTC, T0, load_fixture

D = Decimal


# ------------------------------------------------------------------ registry
def test_registry_maps_ccxt_markets() -> None:
    markets = load_fixture("binanceusdm_markets.json")
    perp = instrument_from_ccxt_market("binanceusdm", markets["BTC/USDT:USDT"], 4)
    assert perp is not None and perp.kind is InstrumentKind.PERP and perp.is_perp
    assert perp.tick_size == D("0.1") and perp.qty_step == D("0.001") and perp.settle_ccy == "USDT"
    assert instrument_from_ccxt_market("binanceusdm", markets["BTC/USD:BTC"], 4) is None  # inverse unsupported
    spot = instrument_from_ccxt_market("binanceusdm", markets["BTC/USDT"], 4)
    assert spot is not None and spot.kind is InstrumentKind.SPOT and spot.settle_ccy == "USDT"
    fut = instrument_from_ccxt_market("binanceusdm", markets["BTC/USDT:USDT-261225"], 4)
    assert fut is not None and fut.kind is InstrumentKind.FUT and fut.expiry == datetime(2026, 12, 25, 8, tzinfo=UTC)
    opt = instrument_from_ccxt_market("binanceusdm", markets["BTC/USDT:USDT-261225-70000-C"], 4)
    assert opt is not None and opt.kind is InstrumentKind.CE and opt.strike == D("70000") and opt.is_option
    assert instrument_from_ccxt_market("x", {**markets["BTC/USDT"], "type": "weird"}, 4) is None


def test_registry_decimal_places_precision_mode() -> None:
    m = {**load_fixture("binanceusdm_markets.json")["BTC/USDT:USDT"], "precision": {"amount": 3, "price": 1}}
    inst = instrument_from_ccxt_market("x", m, 2)
    assert inst is not None and inst.qty_step == D("0.001") and inst.tick_size == D("0.1")
    with pytest.raises(ValueError):
        instrument_from_ccxt_market("x", m, 3)
    with pytest.raises(ValueError):
        instrument_from_ccxt_market("x", {**m, "precision": {}}, 4)


def test_registry_persists_and_reloads() -> None:
    db = Database("sqlite://")
    db.create_all()
    reg = InstrumentRegistry(db)
    got = reg.load_ccxt_markets(
        "binanceusdm",
        load_fixture("binanceusdm_markets.json"),
        4,
        ["BTC/USDT:USDT", "NOPE/USDT:USDT", "BTC/USD:BTC"],
        T0,
    )
    assert [i.symbol for i in got] == ["BTC/USDT:USDT"]
    reg2 = InstrumentRegistry(db)
    assert reg2.load_from_db() == 1
    assert reg2.get(BTC) == reg.get(BTC)
    assert BTC in reg2 and len(reg2.all()) == 1
    with pytest.raises(KeyError):
        reg2.get("nope")
    assert InstrumentRegistry().load_from_db() == 0


# ------------------------------------------------------------------ candles
def c1(minute: int, o: str, h: str, low: str, c: str, v: str = "1", start: datetime = T0) -> Candle:
    return Candle(
        instrument_id=BTC,
        timeframe="1m",
        ts_open=start + timedelta(minutes=minute),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D(v),
    )


def test_timeframe_helpers() -> None:
    assert tf_seconds("15m") == 900 and tf_seconds("4h") == 14400 and tf_seconds("1D") == 86400
    with pytest.raises(ValueError):
        tf_seconds("x")
    assert bucket_start(T0 + timedelta(minutes=7, seconds=3), "5m") == T0 + timedelta(minutes=5)
    ist_offset = timedelta(hours=-5, minutes=-30)
    assert bucket_start(datetime(2026, 1, 5, 3, 0, tzinfo=UTC), "1d", ist_offset) == datetime(
        2026, 1, 4, 18, 30, tzinfo=UTC
    )


def test_aggregates_5m_and_emits_only_closed() -> None:
    b = CandleBuilder(timeframes=["5m"])
    out: list[Candle] = []
    prices = [
        ("100", "105", "99", "104"),
        ("104", "106", "103", "105"),
        ("105", "105", "98", "99"),
        ("99", "101", "97", "100"),
        ("100", "102", "100", "101"),
    ]
    for i, p in enumerate(prices):
        out += b.on_1m_close(c1(i, *p))
    five = [c for c in out if c.timeframe == "5m"]
    assert len(five) == 1  # closes exactly when the 5th minute closes, not before
    k = five[0]
    assert (k.open, k.high, k.low, k.close, k.volume) == (D("100"), D("106"), D("97"), D("101"), D("5"))
    assert k.ts_open == T0 and k.closed
    assert b.last_closed(BTC, "5m") == k and len(b.closed(BTC, "1m", limit=2)) == 2


def test_gap_closes_previous_bucket_and_duplicates_ignored() -> None:
    b = CandleBuilder(timeframes=["5m"])
    b.on_1m_close(c1(0, "1", "2", "1", "2"))
    b.on_1m_close(c1(1, "2", "3", "2", "3"))
    assert b.on_1m_close(c1(1, "9", "9", "9", "9")) == []  # duplicate minute
    out = b.on_1m_close(c1(6, "5", "6", "5", "6"))  # jumped into the next bucket
    assert [c.timeframe for c in out] == ["1m", "5m"] and out[1].close == D("3")
    with pytest.raises(ValueError):
        b.on_1m_close(c1(7, "1", "1", "1", "1").model_copy(update={"timeframe": "5m"}))


def test_forming_candle_from_ticks_and_rollover() -> None:
    b = CandleBuilder(timeframes=[], close_on_tick_rollover=True)
    for s, px in [(0, "100"), (20, "103"), (40, "99")]:
        assert b.on_tick(Tick(instrument_id=BTC, ts=T0 + timedelta(seconds=s), ltp=D(px), volume=D("1"))) == []
    f = b.forming(BTC)
    assert f is not None and not f.closed and (f.high, f.low, f.close, f.volume) == (D("103"), D("99"), D("99"), 3)
    closed = b.on_tick(Tick(instrument_id=BTC, ts=T0 + timedelta(seconds=61), ltp=D("101")))
    assert len(closed) == 1 and closed[0].close == D("99") and closed[0].closed
    assert b.forming("other") is None and b.closed("other", "1m") == [] and b.last_closed("other", "1m") is None


def test_ticks_without_rollover_do_not_close_candles() -> None:
    b = CandleBuilder(timeframes=[])
    b.on_tick(Tick(instrument_id=BTC, ts=T0, ltp=D("100")))
    assert b.on_tick(Tick(instrument_id=BTC, ts=T0 + timedelta(minutes=2), ltp=D("101"))) == []
    assert b.closed(BTC, "1m") == []


# ------------------------------------------------------------------ market state / hub / watchdog
def test_market_state_ignores_older_ticks() -> None:
    s = MarketState()
    s.update(Tick(instrument_id=BTC, ts=T0 + timedelta(seconds=5), ltp=D("2")), T0)
    s.update(Tick(instrument_id=BTC, ts=T0, ltp=D("1")), T0)
    last = s.last(BTC)
    assert last is not None and last.ltp == D("2")
    assert s.age_seconds(BTC, T0 + timedelta(seconds=3)) == 3
    assert s.is_stale("nope", T0, 10) and s.instrument_ids() == [BTC] and s.last_seen(BTC) == T0


async def test_hub_publishes_and_persists() -> None:
    db = Database("sqlite://")
    db.create_all()
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(Topic.TICK, lambda t: seen.append("tick"))
    bus.subscribe(Topic.CANDLE, lambda c: seen.append(c.timeframe))
    hub = MarketHub(ReplayClock(T0), bus, MarketState(), CandleBuilder(timeframes=["5m"]), db)
    hub.seed_history([c1(i, "1", "1", "1", "1", start=T0 - timedelta(minutes=10)) for i in range(5)])
    await hub.on_tick(Tick(instrument_id=BTC, ts=T0, ltp=D("1")))
    for i in range(5):
        await hub.on_1m_candle(c1(i, "1", "2", "1", "2"))
    assert seen[0] == "tick" and seen.count("1m") == 5 and seen.count("5m") == 1
    with db.session() as s:
        assert s.query(CandleRow).count() == 10


async def test_watchdog_transitions() -> None:
    clock = ReplayClock(T0)
    bus = EventBus()
    events: list[tuple[str, Any]] = []
    bus.subscribe(Topic.DATA_STALE, lambda p: events.append(("stale", p)))
    bus.subscribe(Topic.DATA_FRESH, lambda p: events.append(("fresh", p)))
    state = MarketState()
    wd = StalenessWatchdog(clock, bus, state, 10)
    wd.watch([BTC])
    await wd.check()
    assert events[-1][0] == "stale" and wd.is_stale(BTC)  # never received a tick
    state.update(Tick(instrument_id=BTC, ts=T0, ltp=D("1")), clock.now())
    await wd.check()
    assert events[-1][0] == "fresh"
    clock.advance(10)
    await wd.check()
    assert events[-1][0] == "fresh"  # exactly at threshold is still fresh
    clock.advance(1)
    await wd.check()
    await wd.check()  # no duplicate event
    assert [e[0] for e in events] == ["stale", "fresh", "stale"]
    assert wd.snapshot()[BTC]["stale"] is True


# ------------------------------------------------------------------ ccxt provider with a fake exchange
class FakeExchange:
    """Mimics the public part of a ccxt.pro exchange using recorded fixtures."""

    precisionMode = 4
    apiKey = ""
    secret = ""

    def __init__(self, clock: ReplayClock, ws_fail: bool = False, has_bids_asks: bool = True) -> None:
        self.clock = clock
        self.markets = load_fixture("binanceusdm_markets.json")
        self.rows = load_fixture("binanceusdm_btc_ohlcv_1m.json")
        self.ticker = load_fixture("binanceusdm_btc_ticker.json")
        self.funding = load_fixture("binanceusdm_btc_funding.json")
        self.has = {"fetchBidsAsks": has_bids_asks, "watchBidsAsks": False}
        self.ws_fail = ws_fail
        self.closed = False
        self.calls: list[str] = []

    async def load_markets(self, reload: bool = False) -> dict[str, Any]:
        self.calls.append("load_markets")
        return self.markets

    async def fetch_ohlcv(self, symbol: str, tf: str, since: int | None, limit: int) -> list[list[Any]]:
        now_ms = self.clock.now().timestamp() * 1000
        rows = [r for r in self.rows if r[0] <= now_ms and (since is None or r[0] >= since)]
        return rows[-limit:]

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self.ticker

    async def fetch_bids_asks(self, symbols: list[str]) -> dict[str, Any]:
        return {s: {"bid": 64999.9, "ask": 65000.0} for s in symbols}

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        return {"bids": [[64999.8, 1]], "asks": [[65000.1, 1]]}

    async def fetch_funding_rate(self, symbol: str) -> dict[str, Any]:
        return self.funding

    async def watch_ticker(self, symbol: str) -> dict[str, Any]:
        if self.ws_fail:
            raise ConnectionError("ws down")
        await asyncio.sleep(0.01)
        return {"last": 65000.0, "bid": 64999.9, "ask": 65000.0}

    async def close(self) -> None:
        self.closed = True


def make_provider(clock: ReplayClock, **kw: Any) -> tuple[CcxtProvider, MarketHub, FakeExchange]:
    fake = FakeExchange(clock, **kw)
    hub = MarketHub(clock, EventBus(), MarketState(), CandleBuilder(timeframes=["5m"]))
    cfg = CryptoDataConfig(symbols=["BTC/USDT:USDT"], history_candles=50, feed="poll")
    p = CcxtProvider(cfg, clock, hub, InstrumentRegistry(), exchange_factory=lambda _id: fake)
    return p, hub, fake


def test_ohlcv_conversion_drops_forming_bar() -> None:
    rows = load_fixture("binanceusdm_btc_ohlcv_1m.json")[:3]
    now = datetime.fromtimestamp(rows[2][0] / 1000, UTC) + timedelta(seconds=30)
    out = ohlcv_to_candles(BTC, "1m", rows, now, 60)
    assert len(out) == 2  # third bar still forming at `now`


async def test_ccxt_provider_seed_poll_candles_and_quotes() -> None:
    clock = ReplayClock(datetime(2026, 1, 5, 9, 30, 30, tzinfo=UTC))
    p, hub, fake = make_provider(clock)
    p._ex = None
    from papermind.data.public_only import PublicOnlyExchange

    p._ex = PublicOnlyExchange(fake)
    markets = await p.ex.load_markets()
    insts = p.registry.load_ccxt_markets(p.exchange, markets, 4, p.cfg.symbols, clock.now())
    p._instruments = {i.symbol: i for i in insts}
    await p._seed_history(insts[0])
    closed = hub.builder.closed(BTC, "1m")
    assert closed[-1].ts_open == datetime(2026, 1, 5, 9, 29, tzinfo=UTC)  # 09:30 bar still forming
    assert len(closed) == 30
    clock.advance(minutes=3)
    await p.poll_candles_once()
    assert hub.builder.closed(BTC, "1m")[-1].ts_open == datetime(2026, 1, 5, 9, 32, tzinfo=UTC)
    await p.poll_quotes_once()
    t = hub.state.last(BTC)
    assert t is not None and t.bid == D("64999.9") and t.ask == D("65000") and t.ltp == D("65000")
    assert "ccxt:binanceusdm" in t.source
    assert p.status()["instruments"] == [BTC]


async def test_ccxt_provider_order_book_fallback_when_no_bids_asks() -> None:
    clock = ReplayClock(T0)
    p, hub, fake = make_provider(clock, has_bids_asks=False)
    from papermind.data.public_only import PublicOnlyExchange

    p._ex = PublicOnlyExchange(fake)
    insts = p.registry.load_ccxt_markets(p.exchange, fake.markets, 4, p.cfg.symbols, clock.now())
    p._instruments = {i.symbol: i for i in insts}
    await p.poll_quotes_once()
    t = hub.state.last(BTC)
    assert t is not None and t.bid == D("64999.8") and t.ask == D("65000.1")


async def test_ccxt_funding_emitted_once_when_due() -> None:
    clock = ReplayClock(datetime(2026, 1, 5, 15, 59, tzinfo=UTC))
    p, hub, fake = make_provider(clock)
    from papermind.data.public_only import PublicOnlyExchange

    p._ex = PublicOnlyExchange(fake)
    insts = p.registry.load_ccxt_markets(p.exchange, fake.markets, 4, p.cfg.symbols, clock.now())
    p._instruments = {i.symbol: i for i in insts}
    got: list[Any] = []
    hub.bus.subscribe(Topic.FUNDING, got.append)
    await p.poll_funding_once()
    await p.emit_due_funding()
    assert got == []
    clock.set(datetime(2026, 1, 5, 16, 0, 1, tzinfo=UTC))
    await p.emit_due_funding()
    await p.poll_funding_once()  # same timestamp returned again: must not double-charge
    assert len(got) == 1 and got[0].rate == D("0.0001") and got[0].mark_price == D("65010")


async def test_ccxt_provider_full_lifecycle_and_ws_fallback() -> None:
    clock = ReplayClock(datetime(2026, 1, 5, 9, 30, 30, tzinfo=UTC))
    p, _hub, fake = make_provider(clock, ws_fail=True)
    p.cfg = p.cfg.model_copy(update={"feed": "ws"})
    p.feed_mode = "ws"
    await p.start()
    assert p.connected and p.instrument_ids() == [BTC]
    for _ in range(10):  # let the ws loop fail -> backoff sleeps on the replay clock
        for _ in range(5):
            await asyncio.sleep(0)
        clock.advance(70)
    assert p.feed_mode == "poll" and p.errors >= 3 and "ws down" in (p.last_error or "")
    await p.refresh_instruments()
    await p.stop()
    assert fake.closed and not p.connected


async def test_ccxt_provider_seed_error_is_recorded_not_raised() -> None:
    clock = ReplayClock(T0)
    p, _hub, fake = make_provider(clock)

    async def boom(*a: Any, **k: Any) -> Any:
        raise TimeoutError("slow")

    fake.fetch_ohlcv = boom  # type: ignore[method-assign]
    await p.start()
    assert p.errors >= 1 and "seed history" in (p.last_error or "")
    await p.stop()
    with pytest.raises(RuntimeError):
        _ = CcxtProvider(p.cfg, clock, p.hub, p.registry).ex


# ------------------------------------------------------------------ simulated provider
async def test_simulated_provider_is_deterministic() -> None:
    async def once() -> list[Decimal]:
        clock = ReplayClock(T0)
        hub = MarketHub(clock, EventBus(), MarketState(), CandleBuilder(timeframes=["5m"], close_on_tick_rollover=True))
        cfg = CryptoDataConfig(
            provider="simulated",
            symbols=["BTC/USDT:USDT"],
            history_candles=30,
            seed_candles=2,
            simulated=SimulatedFeedConfig(start_prices={"BTC/USDT:USDT": D("65000")}),
        )
        p = SimulatedProvider(cfg, clock, hub, InstrumentRegistry())
        await p.start()
        prices: list[Decimal] = []
        for _ in range(5):
            clock.advance(1)
            await p.tick_once()
            t = hub.state.last("crypto:simulated:BTC/USDT:USDT")
            assert t is not None and t.bid is not None and t.ask is not None and t.bid < t.ask
            prices.append(t.ltp)
        hist = hub.builder.closed("crypto:simulated:BTC/USDT:USDT", "1m")
        assert len(hist) == 30 and hist[-1].close == D("65000")
        assert await p.fetch_candles("crypto:simulated:BTC/USDT:USDT", "1m", since=T0 - timedelta(minutes=5))
        assert p.status()["feed_mode"] == "simulated"
        await p.stop()
        return prices

    a, b = await once(), await once()
    assert a == b and len(set(a)) > 1


async def test_simulated_funding_every_8h() -> None:
    clock = ReplayClock(datetime(2026, 1, 5, 7, 59, 59, tzinfo=UTC))
    bus = EventBus()
    got: list[Any] = []
    bus.subscribe(Topic.FUNDING, got.append)
    hub = MarketHub(clock, bus, MarketState(), CandleBuilder(timeframes=[]))
    cfg = CryptoDataConfig(provider="simulated", symbols=["ETH/USDT:USDT"], history_candles=2)
    p = SimulatedProvider(cfg, clock, hub, InstrumentRegistry())
    await p.start()
    clock.advance(2)
    await p.tick_once()
    await p.tick_once()
    await p.stop()
    assert len(got) == 1 and got[0].ts == datetime(2026, 1, 5, 8, tzinfo=UTC)
    assert SimulatedProvider._next_funding(datetime(2026, 1, 5, 23, 0, tzinfo=UTC)) == datetime(2026, 1, 6, tzinfo=UTC)


def test_seed_timeframe_then_aggregation_dedupes() -> None:
    b = CandleBuilder(timeframes=["5m"])
    higher = [c1(0, "1", "9", "1", "5").model_copy(update={"timeframe": "5m"})]
    b.seed_timeframe(higher)
    b.seed([c1(i, "1", "2", "1", "2") for i in range(5)])  # aggregates the same 5m bucket
    got = b.closed(BTC, "5m")
    assert len(got) == 1 and got[0].high == D("9")  # the authoritative seeded bar wins


async def test_ccxt_provider_seeds_signal_timeframes() -> None:
    clock = ReplayClock(datetime(2026, 1, 5, 11, 0, tzinfo=UTC))
    p, hub, _fake = make_provider(clock)
    p.cfg = p.cfg.model_copy(update={"seed_timeframes": ["15m"], "seed_candles": 100})
    await p.start()
    try:
        assert len(hub.builder.closed(BTC, "15m")) >= 4  # 2h of fixture 1m data served as 15m by the fake
        assert len(hub.builder.closed(BTC, "1m")) > 0
    finally:
        await p.stop()
