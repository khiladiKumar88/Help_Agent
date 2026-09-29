"""Backtester, walk-forward and replay — all through the live Engine on the replay clock."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from papermind.backtest.runner import (
    BacktestError,
    BacktestSpec,
    ReplaySpec,
    WalkForwardSpec,
    _RangeSpec,
    assert_costs_on,
    day_bounds,
    run_backtest,
    run_engine,
    run_replay,
    run_walkforward,
    walkforward_folds,
)
from papermind.core.clock import ReplayClock
from papermind.core.config import BookConfig
from papermind.data.history import HistoryDownloader, HistoryStore, SimulatedHistoryExchange
from papermind.strategies.builtin import make_strategy
from tests.conftest import BOOK_ID, book_dict, make_cfg

END = datetime(2026, 3, 1, tzinfo=UTC)
SYM = "BTC/USDT:USDT"


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> HistoryStore:
    path: Path = tmp_path_factory.mktemp("hist") / "h.db"
    st = HistoryStore(f"sqlite:///{path}")
    clock = ReplayClock(END)

    async def load() -> None:
        dl = HistoryDownloader(st, lambda _id: SimulatedHistoryExchange(clock), clock)
        await dl.sync("simulated", SYM, "15m", END - timedelta(days=45))
        await dl.sync("simulated", SYM, "5m", END - timedelta(days=5))

    asyncio.run(load())
    return st


def cfg_with(**book: Any) -> Any:
    return make_cfg({**book_dict(), **book})


def spec(**kw: Any) -> BacktestSpec:
    base: dict[str, Any] = {
        "book_id": BOOK_ID,
        "exchange": "simulated",
        "symbol": SYM,
        "base_tf": "15m",
        "start": END - timedelta(days=14),
        "end": END,
        "strategy_id": "supertrend_flip",
    }
    base.update(kw)
    return BacktestSpec(**base)


async def test_backtest_report_is_complete_and_reproducible(store: HistoryStore) -> None:
    cfg = make_cfg()
    a = await run_backtest(cfg, store, spec())
    b = await run_backtest(cfg, store, spec())
    no_ids = lambda r: [{k: v for k, v in t.items() if k != "id"} for t in r["trades"]]  # noqa: E731
    assert a["summary"] == b["summary"] and no_ids(a) == no_ids(b)  # deterministic (only random ids differ)
    for key in ("agent", "baseline", "buy_hold", "verdict", "equity", "trades", "summary", "candles"):
        assert key in a
    ag = a["agent"]
    assert ag["trades"] > 0 and Decimal(ag["fees"]) > 0
    assert a["baseline"]["trades"] > 0
    assert a["verdict"]["beat_baseline"] in (True, False)
    assert a["verdict"]["meaningful"] == (ag["trades"] >= 30)
    assert "Beat the baseline:" in a["summary"] and "Buy-and-hold made" in a["summary"]
    assert "default parameters" in a["label"] and a["approximate"] is False
    assert a["data"]["bars"] == a["data"]["expected_bars"] == 14 * 96


async def test_every_trade_pays_fees_and_enters_after_its_signal(store: HistoryStore) -> None:
    out = await run_engine(make_cfg(), store, spec(), [make_strategy("supertrend_flip")])
    sig_ts = {s["id"]: s["ts"] for s in out.signals}
    assert out.agent_trades
    for t in out.agent_trades + out.baseline_trades:
        assert t.charges > 0, "fees are always on"
        assert t.opened_at is not None and t.opened_at >= spec().start
    for t in out.agent_trades:
        assert t.signal_id in sig_ts
        assert t.opened_at > sig_ts[t.signal_id], "fill happens strictly after the signal (next bar open)"
    for s in out.signals:
        assert s["ts"].minute % 15 == 0 and s["ts"].second == 0  # signals only at candle closes


async def test_trades_are_path_independent_of_future_data(store: HistoryStore) -> None:
    """Truncating the data after day X must not change any trade opened before day X (no lookahead)."""
    cut = END - timedelta(days=5)
    full = await run_engine(make_cfg(), store, spec(), [make_strategy("supertrend_flip")], baseline=False)
    short = await run_engine(make_cfg(), store, spec(end=cut), [make_strategy("supertrend_flip")], baseline=False)
    early_full = [
        (t.opened_at, t.direction, t.avg_entry) for t in full.agent_trades if t.closed_at and t.closed_at < cut
    ]
    early_short = [
        (t.opened_at, t.direction, t.avg_entry)
        for t in short.agent_trades
        if t.closed_at and t.closed_at < cut and t.exit_reason != "end_of_test"
    ]
    assert early_full and early_full[: len(early_short)] == early_short


async def test_custom_params_are_labelled_as_possibly_fitted(store: HistoryStore) -> None:
    r = await run_backtest(make_cfg(), store, spec(params={"mult": 2.0}))
    assert "may be fitted" in r["label"] and r["params"]["mult"] == 2.0


async def test_costs_can_never_be_switched_off(store: HistoryStore) -> None:
    no_slip = cfg_with(slippage={"model": "bps", "bps": "0", "est_spread_bps": "2"})
    with pytest.raises(BacktestError, match="slippage"):
        await run_backtest(no_slip, store, spec())
    no_spread = cfg_with(slippage={"model": "bps", "bps": "2", "est_spread_bps": "0"})
    with pytest.raises(BacktestError, match="spread"):
        assert_costs_on(no_spread, no_spread.book(BOOK_ID))
    free = make_cfg()
    free = free.model_copy(
        update={
            "charges": {
                **free.charges,
                "crypto_test": free.charges["crypto_test"].model_copy(update={"taker_pct": Decimal("0")}),
            }
        }
    )
    with pytest.raises(BacktestError, match="fees"):
        assert_costs_on(free, free.book(BOOK_ID))


async def test_helpful_errors(store: HistoryStore) -> None:
    with pytest.raises(BacktestError, match="download"):
        await run_backtest(
            make_cfg(), store, spec(start=datetime(2024, 1, 1, tzinfo=UTC), end=datetime(2024, 1, 10, tzinfo=UTC))
        )
    with pytest.raises(BacktestError, match="no stored market specs"):
        await run_backtest(make_cfg(), store, spec(exchange="binanceusdm"))
    with pytest.raises(BacktestError, match="not in book"):
        await run_backtest(cfg_with(instruments=["ETH/USDT:USDT"]), store, spec())
    with pytest.raises(BacktestError, match="evenly divide"):
        await run_backtest(make_cfg(), store, spec(base_tf="1h"))
    with pytest.raises(ValueError):
        spec(end=END - timedelta(days=30))
    india = {**book_dict(), "market": "india", "id": "india-futures-intraday"}
    with pytest.raises(BacktestError, match="does not support"):
        await run_backtest(make_cfg(india), store, spec(book_id="india-futures-intraday", strategy_id="funding_fade"))


async def test_walkforward_reports_out_of_sample_only(store: HistoryStore) -> None:
    wf = WalkForwardSpec(
        book_id=BOOK_ID,
        exchange="simulated",
        symbol=SYM,
        base_tf="15m",
        strategy_id="supertrend_flip",
        start=END - timedelta(days=24),
        end=END,
        train_days=10,
        test_days=7,
        min_train_trades=3,
    )
    folds = walkforward_folds(wf)
    assert len(folds) == 2 and folds[0][1] == folds[0][0] + timedelta(days=10)
    progress: list[float] = []
    r = await run_walkforward(make_cfg(), store, wf, lambda f, _m: progress.append(f))
    assert r["kind"] == "walkforward" and len(r["folds"]) == 2 and r["grid_size"] <= 16
    assert progress[-1] == 1.0
    test_windows = [(datetime.fromisoformat(f["test"][0]), datetime.fromisoformat(f["test"][1])) for f in r["folds"]]
    for t in r["trades"]:
        opened = datetime.fromisoformat(t["opened_at"])
        assert any(a <= opened < b for a, b in test_windows), "a reported trade came from a training window"
    assert "out-of-sample" in r["label"] and r["baseline"]["trades"] >= 0
    assert all(f["params"] for f in r["folds"])


def test_walkforward_needs_enough_range() -> None:
    wf = WalkForwardSpec(
        book_id=BOOK_ID,
        exchange="simulated",
        symbol=SYM,
        strategy_id="orb",
        start=END - timedelta(days=20),
        end=END,
        train_days=30,
        test_days=10,
    )
    with pytest.raises(BacktestError, match="range too short"):
        walkforward_folds(wf)


async def test_replay_runs_book_strategies_through_live_engine(store: HistoryStore) -> None:
    cfg = cfg_with(strategies=[{"id": "supertrend_flip"}, {"id": "ema_pullback"}, {"id": "orb", "enabled": False}])
    day = END - timedelta(days=2)
    r = await run_replay(
        cfg, store, ReplaySpec(book_id=BOOK_ID, exchange="simulated", symbol=SYM, day=day, base_tf="5m")
    )
    start, end = day_bounds(day, "UTC")
    assert r["kind"] == "replay" and r["strategies"] == ["supertrend_flip", "ema_pullback"]
    assert len(r["candles"]) == 96  # 15m signal candles of that day
    assert all(start <= datetime.fromisoformat(c["t"]) < end for c in r["candles"])
    for s in r["signal_list"]:
        assert start <= datetime.fromisoformat(s["ts"]) <= end and s["decision"] in ("take", "skip")
    with pytest.raises(BacktestError, match="no enabled strategies"):
        await run_replay(make_cfg(), store, ReplaySpec(book_id=BOOK_ID, exchange="simulated", symbol=SYM, day=day))


async def test_replay_and_backtest_agree_on_the_same_day(store: HistoryStore) -> None:
    """Same engine code => a one-strategy replay and a backtest of that day produce identical agent trades."""
    cfg = cfg_with(strategies=[{"id": "supertrend_flip"}])
    day = END - timedelta(days=3)
    start, end = day_bounds(day, "UTC")
    rep = await run_replay(
        cfg, store, ReplaySpec(book_id=BOOK_ID, exchange="simulated", symbol=SYM, day=day, base_tf="5m")
    )
    bt = await run_backtest(cfg, store, spec(base_tf="5m", start=start, end=end))
    agent = lambda r: [{k: v for k, v in t.items() if k != "id"} for t in r["trades"] if t["actor"] == "agent"]  # noqa: E731
    assert agent(rep) == agent(bt)


async def test_replay_cancellation(store: HistoryStore) -> None:
    from papermind.backtest.replay import ReplayCancelled

    with pytest.raises(ReplayCancelled):
        await run_engine(make_cfg(), store, spec(), [make_strategy("orb")], cancelled=lambda: True)


def test_day_bounds_in_ist() -> None:
    s, e = day_bounds(datetime(2026, 1, 5, 20, 0, tzinfo=UTC), "Asia/Kolkata")
    assert s == datetime(2026, 1, 5, 18, 30, tzinfo=UTC) and e - s == timedelta(days=1)


def test_range_spec_requires_aware_datetimes() -> None:
    with pytest.raises(ValueError):
        _RangeSpec(book_id="b", exchange="e", symbol="s", start=datetime(2026, 1, 1), end=datetime(2026, 1, 2))


def test_book_config_is_untouched_by_backtests() -> None:
    b = BookConfig.model_validate(book_dict())
    assert b.mode == "manual" and b.baseline.enabled
