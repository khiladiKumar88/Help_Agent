from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from papermind.backtest.jobs import JobManager
from papermind.backtest.metrics import cumulative_series, trade_metrics
from papermind.backtest.options import (
    APPROXIMATE_NOTE,
    SyntheticOptionConfig,
    SyntheticOptionFeed,
    black_scholes,
    synthetic_quote,
    volatility_from,
)
from papermind.backtest.runner import BacktestError, plain_english, verdict
from papermind.core.config import Market, Segment
from papermind.core.types import Account, Actor, Direction, Instrument, InstrumentKind, Tick, TradeStatus
from papermind.db.base import Database
from papermind.db.models import BacktestRunRow
from papermind.journal.entities import Trade
from tests.conftest import T0

D = Decimal


def trade(net: str, r: str | None, day: int = 0, fees: str = "0.5", hold_min: int = 30) -> Trade:
    opened = T0 + timedelta(days=day)
    return Trade(
        id=f"t{net}{day}",
        book_id="b",
        account=Account.MAIN,
        actor=Actor.AGENT,
        instrument_id="i",
        direction=Direction.LONG,
        status=TradeStatus.CLOSED,
        qty=D(1),
        contract_size=D(1),
        leverage=D(1),
        initial_sl=D(1),
        current_sl=D(1),
        created_at=opened,
        net_pnl=D(net),
        charges=D(fees),
        r_multiple=D(r) if r else None,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=hold_min),
    )


def test_trade_metrics_hand_computed() -> None:
    trades = [trade("20", "2"), trade("-10", "-1", 1), trade("-10", "-1", 2), trade("5", "0.5", 3)]
    m = trade_metrics(trades, D("1000"))
    assert (m.trades, m.wins, m.losses) == (4, 2, 2)
    assert m.win_rate == 0.5 and m.net_profit == D("5") and m.gross_profit == D("25") and m.gross_loss == D("-20")
    assert m.profit_factor == 1.25 and m.avg_r == 0.125 and m.fees == D("2.0")
    assert m.max_drawdown == D("20") and m.max_drawdown_pct == pytest.approx(1.961, abs=1e-3)
    assert m.return_pct == 0.5 and m.sharpe is None  # < 5 trading days
    assert m.best_trade == D("20") and m.worst_trade == D("-10") and m.avg_hold_minutes == 30.0
    d = m.as_dict()
    assert d["net_profit"] == "5" and isinstance(d["win_rate"], float)


def test_metrics_edge_cases() -> None:
    empty = trade_metrics([], D("1000"))
    assert empty.trades == 0 and empty.profit_factor is None and empty.avg_r is None and empty.win_rate == 0
    winners = trade_metrics([trade("5", "1", d) for d in range(6)], D("1000"))
    assert winners.profit_factor is None and winners.max_drawdown == 0
    mixed = trade_metrics([trade("5", "1", d) if d % 2 else trade("-3", "-1", d) for d in range(10)], D("1000"))
    assert mixed.sharpe is not None and mixed.sharpe > 0
    series = cumulative_series([trade("5", "1", 1), trade("-2", None, 0)])
    assert [p["v"] for p in series] == ["-2", "3"]


def test_verdict_and_plain_english() -> None:
    agent = trade_metrics([trade("10", "1", d) for d in range(40)], D("1000"))
    base = trade_metrics([trade("-1", "-0.1", d) for d in range(40)], D("1000"))
    v = verdict(agent, base, None, 30)
    assert v == {"meaningful": True, "min_trades": 30, "beat_baseline": True, "beat_buy_hold": False}
    text = plain_english("Test", agent, base, None, v, "USDT")
    assert "net profit +400.00 USDT" in text and "Beat the baseline: YES" in text and "meaningful" in text
    few = trade_metrics([trade("10", "1")], D("1000"))
    v2 = verdict(few, base, None, 30)
    t2 = plain_english("Few", few, base, None, v2, "USDT", approximate=True)
    assert "NOT meaningful" in t2 and "APPROXIMATE" in t2
    none = trade_metrics([], D("1000"))
    assert "took no trades" in plain_english("X", none, None, None, verdict(none, None, None, 30), "INR")
    assert verdict(none, base, None, 30)["beat_baseline"] is False


# ------------------------------------------------------------------ options


def test_black_scholes_textbook_values_and_parity() -> None:
    c = black_scholes(100, 100, 1.0, 0.05, 0.2, call=True)
    p = black_scholes(100, 100, 1.0, 0.05, 0.2, call=False)
    assert c.price == pytest.approx(10.4506, abs=1e-4)
    assert p.price == pytest.approx(5.5735, abs=1e-4)
    assert c.price - p.price == pytest.approx(100 - 100 * math.exp(-0.05), abs=1e-9)  # put-call parity
    assert 0 < c.delta < 1 and -1 < p.delta < 0 and c.delta - p.delta == pytest.approx(1.0)
    assert c.gamma == pytest.approx(p.gamma) and c.vega > 0 and c.theta < 0
    assert black_scholes(110, 100, 0, 0.05, 0.2, True).price == 10  # expiry = intrinsic
    assert black_scholes(90, 100, 0, 0.05, 0.2, False).delta == -1.0
    assert black_scholes(90, 100, 0.5, 0.05, 0.2, True, div_yield=0.01).price > 0
    with pytest.raises(ValueError):
        black_scholes(0, 100, 1, 0.05, 0.2, True)


def test_volatility_source_priority() -> None:
    cfg = SyntheticOptionConfig()
    assert volatility_from(14.5, 0.5, cfg) == pytest.approx(0.145)  # VIX wins
    assert volatility_from(None, 0.2, cfg) == pytest.approx(0.22)  # realized x 1.1
    assert volatility_from(None, None, cfg) == cfg.vol_floor
    assert volatility_from(500.0, None, cfg) == cfg.vol_cap


def nifty_ce(expiry: datetime) -> Instrument:
    return Instrument(
        id="india:NFO:NIFTY-CE",
        market=Market.INDIA,
        segment=Segment.OPTIONS,
        exchange="NFO",
        symbol="NIFTY26JAN25000CE",
        underlying="NIFTY",
        kind=InstrumentKind.CE,
        quote_ccy="INR",
        settle_ccy="INR",
        lot_size=D(75),
        tick_size=D("0.05"),
        qty_step=D(75),
        min_qty=D(75),
        strike=D(25000),
        expiry=expiry,
    )


def test_synthetic_quote_spread_and_ticks() -> None:
    inst = nifty_ce(T0 + timedelta(days=7))
    q = synthetic_quote(inst, D(25000), T0, 0.14, SyntheticOptionConfig(spread_pct=2.0))
    assert q.approximate and q.bid < q.mid < q.ask
    assert q.bid % inst.tick_size == 0 and q.ask % inst.tick_size == 0
    assert (q.ask - q.bid) >= inst.tick_size * 2 and 0.4 < q.delta < 0.6
    deep_otm = synthetic_quote(nifty_ce(T0 + timedelta(hours=1)), D(20000), T0, 0.1, SyntheticOptionConfig())
    assert deep_otm.bid == inst.tick_size and deep_otm.ask > deep_otm.bid  # never below one tick
    with pytest.raises(ValueError):
        synthetic_quote(inst.model_copy(update={"strike": None}), D(1), T0, 0.1, SyntheticOptionConfig())
    assert "approximate" in APPROXIMATE_NOTE


async def test_synthetic_feed_lets_the_broker_trade_an_option() -> None:
    from papermind.broker.paper_broker import PaperBroker
    from papermind.core.clock import ReplayClock
    from papermind.core.events import EventBus
    from papermind.core.types import OrderRequest
    from papermind.data.market import MarketState
    from papermind.instruments.registry import InstrumentRegistry
    from papermind.journal.service import Journal
    from tests.conftest import make_cfg

    book = {
        "id": "india-options-intraday",
        "market": "india",
        "segment": "options",
        "style": "intraday",
        "currency": "INR",
        "starting_capital": "20000",
        "data_source": "india",
        "instruments": ["NIFTY26JAN25000CE"],
        "charges": "india_test",
        "slippage": {"model": "spread", "extra_ticks": 1, "est_spread_bps": "20"},
        "risk": {"max_risk_per_trade_abs": "2000", "max_lots": 1, "max_premium": "150", "max_open_positions": 1},
        "baseline": {"enabled": False},
    }
    cfg = make_cfg(book)
    clock = ReplayClock(T0)
    db = Database("sqlite://")
    db.create_all()
    bus = EventBus()
    reg = InstrumentRegistry(db)
    inst = nifty_ce(T0 + timedelta(days=2))
    reg.upsert([inst], T0)
    market = MarketState()
    broker = PaperBroker(cfg, clock, bus, Journal(db, clock), reg, market)
    broker.start()

    async def publish(t: Tick) -> None:
        market.update(t, clock.now())
        await broker.on_tick(t)

    feed = SyntheticOptionFeed(bus, publish, [inst], "india:NSE:NIFTY", lambda _ts: 0.13)

    async def underlying(px: str) -> None:
        from papermind.core.events import Topic

        clock.advance(1)
        await bus.publish(Topic.TICK, Tick(instrument_id="india:NSE:NIFTY", ts=clock.now(), ltp=D(px)))

    await underlying("24950")
    q = market.last(inst.id)
    assert q is not None and q.source == "synthetic_bs" and q.ask is not None and q.ask < 150
    d, t = await broker.submit(
        OrderRequest(
            book_id="india-options-intraday",
            actor=Actor.HUMAN,
            instrument_id=inst.id,
            direction=Direction.LONG,
            qty=D(75),
            stop_loss=q.bid - 20,
        )
    )
    assert d.approved, d.message
    await underlying("24960")
    assert t is not None and broker._trades[t.id].status is TradeStatus.OPEN
    await underlying("25200")  # rally: option premium rises
    view = broker.trade_view(broker._trades[t.id])
    assert D(view["unrealized_gross"]) > 0 and feed.quotes_emitted == 3


# ------------------------------------------------------------------ jobs


def test_job_manager_success_error_cancel_and_restart() -> None:
    db = Database("sqlite://")
    db.create_all()
    jobs = JobManager(db)

    async def ok(progress: Any, cancelled: Any) -> dict[str, Any]:
        progress(0.5, "half")
        return {"summary": "done!", "verdict": {"meaningful": False}}

    async def bad(progress: Any, cancelled: Any) -> dict[str, Any]:
        raise BacktestError("download data first")

    async def crash(progress: Any, cancelled: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    async def slow(progress: Any, cancelled: Any) -> dict[str, Any]:
        from papermind.backtest.replay import ReplayCancelled

        for _ in range(200):
            if cancelled():
                raise ReplayCancelled()
            await asyncio.sleep(0.01)
        return {}

    r1 = jobs.submit("backtest", {"a": 1}, ok)
    assert jobs.wait(r1)["status"] == "done"  # type: ignore[index]
    full = jobs.get(r1)
    assert full is not None and full["result"]["summary"] == "done!" and full["progress"] == 1.0
    r2 = jobs.submit("backtest", {}, bad)
    assert jobs.wait(r2)["error"] == "download data first"  # type: ignore[index]
    r3 = jobs.submit("backtest", {}, crash)
    assert "RuntimeError" in jobs.wait(r3)["error"]  # type: ignore[index]
    r4 = jobs.submit("replay", {}, slow)
    import time

    time.sleep(0.05)
    assert jobs.cancel(r4)
    assert jobs.wait(r4)["status"] == "cancelled"  # type: ignore[index]
    assert jobs.cancel("nope") is False and jobs.get("nope") is None
    listed = jobs.list()
    assert len(listed) == 4 and "result" not in listed[0] and listed[-1]["summary"] == "done!"
    # a job left "running" by a crash is marked interrupted on the next start
    with db.session() as s:
        s.add(BacktestRunRow(id="run_x", kind="backtest", status="running", progress=0.3, spec={}, created_at=T0))
    JobManager(db)
    assert jobs.get("run_x")["status"] == "error"  # type: ignore[index]
    jobs.shutdown()


def test_now_is_utc() -> None:
    from papermind.backtest.jobs import _now

    assert _now().tzinfo is UTC
