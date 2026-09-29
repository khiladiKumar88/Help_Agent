"""Backtests, walk-forward and replays — all executed by the live Engine on a ReplayClock.

Honesty rules enforced here:
- Data comes only from the local HistoryStore (never the exchange API).
- Fees and slippage are always on (the book's own charge + slippage config); zero costs are refused.
- Parameter search uses the strategy's SMALL grid (<= 16 combos) and only on TRAIN windows.
  Walk-forward results are reported on the OUT-OF-SAMPLE test windows only.
- Every run is compared with the random baseline (same risk + frequency) and buy-and-hold.
- Fewer than `min_trades` trades => "not meaningful".
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from papermind.backtest.metrics import BuyHold, Metrics, cumulative_series, trade_metrics
from papermind.backtest.replay import HistoricalReplayProvider
from papermind.broker.charges import make_charges
from papermind.core.clock import ReplayClock
from papermind.core.config import AppConfig, BaselineConfig, BookConfig, CryptoChargesConfig, Mode, Settings
from papermind.core.money import BPS, ZERO, round_to_step
from papermind.core.serde import jdict
from papermind.core.types import Account, Actor, Candle, ExitReason, Instrument, Liquidity, Side
from papermind.data.candles import CandleBuilder, tf_seconds
from papermind.data.history import HistoryStore, funding_rate_at
from papermind.instruments.registry import instrument_from_ccxt_market
from papermind.journal.entities import Trade
from papermind.strategies.base import ParamValue, Strategy, expand_grid
from papermind.strategies.builtin import STRATEGIES, make_strategy

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]
MIN_TRADES_DEFAULT = 30


class BacktestError(ValueError):
    """User-facing problem with a backtest request (missing data, bad range, costs off...)."""


# ============================================================================ specs


class _RangeSpec(BaseModel):
    book_id: str
    exchange: str
    symbol: str
    base_tf: str = "5m"
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check(self) -> _RangeSpec:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class BacktestSpec(_RangeSpec):
    strategy_id: str
    params: dict[str, ParamValue] = Field(default_factory=dict)
    min_trades: int = MIN_TRADES_DEFAULT


class WalkForwardSpec(_RangeSpec):
    strategy_id: str
    train_days: int = Field(60, ge=7)
    test_days: int = Field(30, ge=3)
    min_trades: int = MIN_TRADES_DEFAULT
    min_train_trades: int = 10


class ReplaySpec(BaseModel):
    book_id: str
    exchange: str
    symbol: str
    day: datetime  # any time on the day to replay (book's trading-day timezone)
    base_tf: str = "5m"
    speed: float = 0.0  # 0 = as fast as possible; N = N x real time


# ============================================================================ single engine run


@dataclass
class RunOutput:
    instrument: Instrument
    agent_trades: list[Trade]
    baseline_trades: list[Trade]
    equity: dict[str, list[dict[str, Any]]]
    signals: list[dict[str, Any]]
    candles: list[Candle]  # signal-timeframe candles inside [start, end)
    bars_in_window: int
    expected_bars: int
    params: dict[str, ParamValue] = field(default_factory=dict)


def assert_costs_on(cfg: AppConfig, book: BookConfig) -> None:
    ch = cfg.charges[book.charges]
    if isinstance(ch, CryptoChargesConfig) and (ch.taker_pct <= 0 or ch.maker_pct < 0):
        raise BacktestError("backtests require non-zero trading fees (charges config has zero taker fee)")
    s = book.slippage
    if (s.model == "bps" and s.bps <= 0) or (s.model == "spread" and s.extra_ticks <= 0 and s.est_spread_bps <= 0):
        raise BacktestError("backtests require non-zero slippage (book slippage config is zero)")
    if s.est_spread_bps <= 0:
        raise BacktestError("backtests require a non-zero estimated spread (OHLCV data has no bid/ask)")


def load_instrument(store: HistoryStore, exchange: str, symbol: str) -> Instrument:
    spec = store.market(exchange, symbol)
    if spec is None:
        raise BacktestError(f"no stored market specs for {exchange} {symbol} — download history first")
    inst = instrument_from_ccxt_market(exchange, spec, int(spec.get("_precision_mode", 4)))
    if inst is None:
        raise BacktestError(f"{symbol} on {exchange} is not a supported market type")
    return inst


def warmup_duration(strategies: list[Strategy], book: BookConfig) -> timedelta:
    bars = max([s.warmup_bars for s in strategies] + [150]) + 10
    return timedelta(seconds=tf_seconds(book.timeframes["signal"]) * bars)


async def run_engine(
    cfg: AppConfig,
    store: HistoryStore,
    rng: _RangeSpec,
    strategies: list[Strategy],
    baseline: bool = True,
    baseline_seed: int | None = None,
    speed: float = 0.0,
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RunOutput:
    from papermind.engine import Engine, EngineOptions  # local import: engine imports the scanner

    book = cfg.book(rng.book_id)
    assert_costs_on(cfg, book)
    if rng.symbol not in book.instruments:
        raise BacktestError(f"{rng.symbol} is not in book {book.id}")
    signal_tf = book.timeframes["signal"]
    base_s, sig_s = tf_seconds(rng.base_tf), tf_seconds(signal_tf)
    if sig_s < base_s or sig_s % base_s:
        raise BacktestError(f"data timeframe {rng.base_tf} must evenly divide the signal timeframe {signal_tf}")
    inst = load_instrument(store, rng.exchange, rng.symbol)
    if inst.segment is not book.segment:
        raise BacktestError(f"{rng.symbol} is {inst.segment}, but book {book.id} trades {book.segment}")
    data_start = rng.start - warmup_duration(strategies, book)
    candles = store.load_candles(rng.exchange, rng.symbol, rng.base_tf, data_start, rng.end, inst.id)
    in_window = [c for c in candles if c.ts_open >= rng.start]
    if not in_window:
        raise BacktestError(
            f"no stored {rng.base_tf} data for {rng.exchange} {rng.symbol} between {rng.start:%Y-%m-%d} and "
            f"{rng.end:%Y-%m-%d} — download it on the Backtest page first"
        )
    funding = store.load_funding(rng.exchange, rng.symbol, data_start - timedelta(days=1), rng.end)

    seed = baseline_seed if baseline_seed is not None else book.baseline.seed
    book2 = book.model_copy(
        update={
            "mode": Mode.MANUAL,
            "baseline": BaselineConfig(enabled=baseline, seed=seed, max_delay_bars=book.baseline.max_delay_bars),
        }
    )
    cfg2 = cfg.model_copy(update={"books": {book.id: book2}, "db_url": "sqlite://", "equity_snapshot_seconds": 86400})
    clock = ReplayClock(candles[0].ts_open)
    holder: dict[str, HistoricalReplayProvider] = {}

    def provider_factory(engine: Engine) -> HistoricalReplayProvider:
        holder["p"] = HistoricalReplayProvider(
            engine, [inst], {inst.id: candles}, {inst.id: funding} if inst.is_perp else {}, speed, progress, cancelled
        )
        return holder["p"]

    engine = Engine(
        cfg2,
        Settings(_env_file=None),
        clock,
        provider_factory,
        options=EngineOptions(
            drive_timers=False,
            force_execute=True,
            trading_starts_at=rng.start,
            base_tf=rng.base_tf,
            persist_candles=False,
            funding_fn=lambda _i, ts: funding_rate_at(funding, ts),
            strategies={book.id: strategies},
        ),
    )
    engine.builder.timeframes = sorted(set(CandleBuilder().timeframes) | {signal_tf}, key=tf_seconds)
    for s in strategies:
        s.reset()
    await engine.start()
    try:
        await holder["p"].run()
        await engine.broker.flatten_all(ExitReason.END_OF_TEST)
        engine.broker._snapshot(book.id)
        trades = engine.journal.list_trades(book.id, limit=1_000_000)
        agent = [t for t in trades if t.account is Account.MAIN and t.actor is Actor.AGENT and t.closed_at]
        base = [t for t in trades if t.account is Account.SHADOW_BASELINE and t.closed_at]
        equity = {
            "agent": _curve(engine.journal.equity_curve(book.id, Account.MAIN, rng.start)),
            "baseline": _curve(engine.journal.equity_curve(book.id, Account.SHADOW_BASELINE, rng.start))
            if baseline
            else [],
        }
        signals = engine.journal.list_signals(book.id, limit=100_000)
        sig_candles = [c for c in engine.builder.closed(inst.id, signal_tf) if rng.start <= c.ts_open < rng.end]
    finally:
        await engine.stop()
        engine.db.engine.dispose()
    expected = int((min(rng.end, _last_close(in_window)) - rng.start).total_seconds() // base_s)
    return RunOutput(
        inst,
        agent,
        base,
        equity,
        signals,
        sig_candles,
        len(in_window),
        max(expected, len(in_window)),
        dict(strategies[0].params) if len(strategies) == 1 else {},
    )


def _last_close(candles: list[Candle]) -> datetime:
    c = candles[-1]
    return c.ts_open + timedelta(seconds=tf_seconds(c.timeframe))


def _curve(points: list[dict[str, Any]], max_points: int = 400) -> list[dict[str, Any]]:
    step = max(1, math.ceil(len(points) / max_points))
    sampled = points[::step] + ([points[-1]] if points and (len(points) - 1) % step else [])
    return [{"t": p["ts"].isoformat(), "v": format(p["equity"], "f")} for p in sampled]


# ============================================================================ comparisons


def buy_and_hold(
    cfg: AppConfig, book: BookConfig, inst: Instrument, candles: list[Candle], start: datetime, end: datetime
) -> BuyHold | None:
    window = [c for c in candles if start <= c.ts_open < end]
    if not window:
        return None
    charges = make_charges(cfg.charges[book.charges])
    slip = book.slippage.bps + book.slippage.est_spread_bps / 2
    entry = round_to_step(window[0].open * (1 + slip / BPS), inst.tick_size, "up")
    exit_ = round_to_step(window[-1].close * (1 - slip / BPS), inst.tick_size, "down")
    capital = book.starting_capital
    # a benchmark, not an order: fractional quantity so tiny accounts still get a comparable number
    qty = (capital * Decimal("0.98") / (entry * inst.contract_size)).quantize(Decimal("0.00000001"))
    fees = (
        charges.compute(inst, Side.BUY, qty, entry, Liquidity.TAKER).total
        + charges.compute(inst, Side.SELL, qty, exit_, Liquidity.TAKER).total
    )
    net = (exit_ - entry) * qty * inst.contract_size - fees
    peak, max_dd = window[0].open, 0.0
    for c in window:
        peak = max(peak, c.high)
        max_dd = max(max_dd, float((peak - c.low) / peak) * 100.0)
    return BuyHold(start, end, entry, exit_, qty, fees, net, round(float(net / capital) * 100, 3), round(max_dd, 3))


def verdict(agent: Metrics, baseline: Metrics | None, bh: BuyHold | None, min_trades: int) -> dict[str, Any]:
    meaningful = agent.trades >= min_trades
    beat_baseline = None
    if baseline is not None:
        beat_baseline = bool(
            agent.trades > 0
            and agent.net_profit > baseline.net_profit
            and (agent.avg_r or 0.0) > (baseline.avg_r if baseline.avg_r is not None else -math.inf)
        )
    beat_bh = bool(bh is not None and agent.net_profit > bh.net_profit)
    return {
        "meaningful": meaningful,
        "min_trades": min_trades,
        "beat_baseline": beat_baseline,
        "beat_buy_hold": beat_bh,
    }


def _money(v: Decimal, ccy: str) -> str:
    sign = "+" if v > 0 else "−" if v < 0 else ""
    return f"{sign}{abs(v):,.2f} {ccy}"


def plain_english(
    what: str,
    agent: Metrics,
    baseline: Metrics | None,
    bh: BuyHold | None,
    v: dict[str, Any],
    ccy: str,
    approximate: bool = False,
) -> str:
    if agent.trades == 0:
        text = f"{what}: the strategy took no trades, so there is nothing to judge."
    else:
        pf = f"{agent.profit_factor:.2f}" if agent.profit_factor is not None else "n/a (no losing trades)"
        text = (
            f"{what}: net profit {_money(agent.net_profit, ccy)} after {abs(agent.fees):,.2f} {ccy} of fees, over "
            f"{agent.trades} trades. Win rate {agent.win_rate * 100:.0f}%, average {agent.avg_r or 0:+.2f}R per trade, "
            f"profit factor {pf}, max drawdown {agent.max_drawdown_pct:.1f}%."
        )
    if baseline is not None:
        text += (
            f" The random baseline (same risk and frequency) made {_money(baseline.net_profit, ccy)} over "
            f"{baseline.trades} trades ({baseline.avg_r or 0:+.2f}R avg)."
        )
    if bh is not None:
        text += f" Buy-and-hold made {_money(bh.net_profit, ccy)} ({bh.return_pct:+.1f}%)."
    if v["beat_baseline"] is not None:
        text += f" Beat the baseline: {'YES' if v['beat_baseline'] else 'NO'}."
    text += f" Beat buy-and-hold: {'YES' if v['beat_buy_hold'] else 'NO'}."
    if v["meaningful"]:
        text += (
            f" {agent.trades} trades meets the {v['min_trades']}-trade minimum, so the result is statistically "
            "meaningful — but still not proof of a future edge."
        )
    else:
        text += (
            f" Only {agent.trades} trades (minimum {v['min_trades']}), so this result is NOT meaningful yet — "
            "treat it as anecdotal."
        )
    if approximate:
        text += " APPROXIMATE: option prices were synthesized (Black-Scholes), not real quotes."
    return text


def _report(
    kind: str,
    cfg: AppConfig,
    book: BookConfig,
    outs: list[RunOutput],
    windows: list[tuple[datetime, datetime]],
    min_trades: int,
    label: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ccy = book.currency
    ppy = 252.0 if book.market.value == "india" else 365.0
    agent_trades = [t for o in outs for t in o.agent_trades]
    base_trades = [t for o in outs for t in o.baseline_trades]
    agent = trade_metrics(agent_trades, book.starting_capital, ppy)
    base = trade_metrics(base_trades, book.starting_capital, ppy)
    inst = outs[0].instrument
    all_candles = [c for o in outs for c in o.candles]
    bh_parts = [buy_and_hold(cfg, book, inst, o.candles, s, e) for o, (s, e) in zip(outs, windows, strict=True)]
    bh_list = [b for b in bh_parts if b is not None]
    bh = None
    if bh_list:
        net = sum((b.net_profit for b in bh_list), ZERO)
        bh = BuyHold(
            bh_list[0].start,
            bh_list[-1].end,
            bh_list[0].entry,
            bh_list[-1].exit,
            bh_list[0].qty,
            sum((b.fees for b in bh_list), ZERO),
            net,
            round(float(net / book.starting_capital) * 100, 3),
            max(b.max_drawdown_pct for b in bh_list),
        )
    v = verdict(agent, base, bh, min_trades)
    return jdict(
        {
            "kind": kind,
            "label": label,
            "approximate": False,
            "currency": ccy,
            "instrument": inst.id,
            "period": {"start": windows[0][0], "end": windows[-1][1]},
            "data": {"bars": sum(o.bars_in_window for o in outs), "expected_bars": sum(o.expected_bars for o in outs)},
            "agent": agent.as_dict(),
            "baseline": base.as_dict(),
            "buy_hold": bh.as_dict() if bh else None,
            "verdict": v,
            "summary": plain_english(label, agent, base, bh, v, ccy),
            "equity": {"agent": cumulative_series(agent_trades), "baseline": cumulative_series(base_trades)},
            "trades": [
                _trade_row(t) for t in sorted(agent_trades + base_trades, key=lambda t: t.opened_at or t.created_at)
            ][:1000],
            "signals": sum(len(o.signals) for o in outs),
            "candles": [
                {"t": c.ts_open.isoformat(), "o": str(c.open), "h": str(c.high), "l": str(c.low), "c": str(c.close)}
                for c in all_candles
            ][-2000:],
            **(extra or {}),
        }
    )


def _trade_row(t: Trade) -> dict[str, Any]:
    return jdict(
        {
            "id": t.id,
            "actor": t.actor,
            "direction": t.direction,
            "qty": t.qty,
            "entry": t.avg_entry,
            "exit": t.avg_exit,
            "stop": t.initial_sl,
            "target": t.target,
            "opened_at": t.opened_at,
            "closed_at": t.closed_at,
            "net_pnl": t.net_pnl,
            "fees": t.charges,
            "r": t.r_multiple,
            "exit_reason": t.exit_reason,
            "note": t.note[:160],
        }
    )


# ============================================================================ public entry points


async def run_backtest(
    cfg: AppConfig,
    store: HistoryStore,
    spec: BacktestSpec,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    book = cfg.book(spec.book_id)
    strat = make_strategy(spec.strategy_id, spec.params)
    if book.market not in strat.markets:
        raise BacktestError(f"{strat.name} does not support {book.market}")
    out = await run_engine(
        cfg, store, spec, [strat], True, None, 0.0, (lambda f: progress(f, "running")) if progress else None, cancelled
    )
    fitted = bool(spec.params) and spec.params != {k: strat.default_params[k] for k in spec.params}
    label = f"{strat.name}, {spec.start:%Y-%m-%d} → {spec.end:%Y-%m-%d} " + (
        "(your custom parameters — may be fitted to this period)"
        if fitted
        else "(default parameters, not optimized on this data)"
    )
    return _report(
        "backtest",
        cfg,
        book,
        [out],
        [(spec.start, spec.end)],
        spec.min_trades,
        label,
        {"params": out.params, "strategy_id": strat.id},
    )


def walkforward_folds(spec: WalkForwardSpec) -> list[tuple[datetime, datetime, datetime]]:
    folds = []
    train, test = timedelta(days=spec.train_days), timedelta(days=spec.test_days)
    t = spec.start
    while t + train + test <= spec.end:
        folds.append((t, t + train, t + train + test))
        t += test
    if not folds:
        raise BacktestError(
            f"range too short: walk-forward needs at least {spec.train_days + spec.test_days} days "
            f"(train {spec.train_days} + test {spec.test_days})"
        )
    return folds


async def run_walkforward(
    cfg: AppConfig,
    store: HistoryStore,
    spec: WalkForwardSpec,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    book = cfg.book(spec.book_id)
    cls = STRATEGIES.get(spec.strategy_id)
    if cls is None:
        raise BacktestError(f"unknown strategy {spec.strategy_id}")
    if book.market not in cls.markets:
        raise BacktestError(f"{cls.name} does not support {book.market}")
    folds = walkforward_folds(spec)
    grid = expand_grid(cls.param_grid)
    total = len(folds) * (len(grid) + 1)
    done = 0
    outs: list[RunOutput] = []
    fold_rows: list[dict[str, Any]] = []
    for k, (a, b, c) in enumerate(folds):
        scored: list[tuple[float, Decimal, dict[str, ParamValue]]] = []
        for params in grid:
            if progress:
                progress(done / total, f"fold {k + 1}/{len(folds)}: training {params}")
            train = _RangeSpec(
                book_id=spec.book_id, exchange=spec.exchange, symbol=spec.symbol, base_tf=spec.base_tf, start=a, end=b
            )
            o = await run_engine(cfg, store, train, [cls(params)], baseline=False, cancelled=cancelled)
            m = trade_metrics(o.agent_trades, book.starting_capital)
            done += 1
            if m.trades >= spec.min_train_trades and m.avg_r is not None:
                scored.append((m.avg_r, m.net_profit, params))
        if scored:
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            chosen, why = scored[0][2], f"best train avg R {scored[0][0]:+.2f} over {len(scored)} qualifying combos"
        else:
            chosen = dict(cls.default_params)
            why = f"no combo reached {spec.min_train_trades} train trades — using defaults"
        if progress:
            progress(done / total, f"fold {k + 1}/{len(folds)}: out-of-sample test")
        test = _RangeSpec(
            book_id=spec.book_id, exchange=spec.exchange, symbol=spec.symbol, base_tf=spec.base_tf, start=b, end=c
        )
        o = await run_engine(
            cfg, store, test, [cls(chosen)], baseline=True, baseline_seed=book.baseline.seed + k, cancelled=cancelled
        )
        done += 1
        outs.append(o)
        fm = trade_metrics(o.agent_trades, book.starting_capital)
        bm = trade_metrics(o.baseline_trades, book.starting_capital)
        fold_rows.append(
            jdict(
                {
                    "fold": k + 1,
                    "train": [a, b],
                    "test": [b, c],
                    "params": chosen,
                    "why": why,
                    "oos_trades": fm.trades,
                    "oos_net_profit": fm.net_profit,
                    "oos_avg_r": fm.avg_r,
                    "baseline_net_profit": bm.net_profit,
                    "baseline_avg_r": bm.avg_r,
                }
            )
        )
    label = (
        f"Walk-forward, {cls.name}: out-of-sample results only, {len(folds)} test windows of "
        f"{spec.test_days} days (each after {spec.train_days} training days)"
    )
    if progress:
        progress(1.0, "done")
    return _report(
        "walkforward",
        cfg,
        book,
        outs,
        [(f[1], f[2]) for f in folds],
        spec.min_trades,
        label,
        {"folds": fold_rows, "strategy_id": cls.id, "grid_size": len(grid)},
    )


def day_bounds(day: datetime, tz: str) -> tuple[datetime, datetime]:
    local = day.astimezone(ZoneInfo(tz)).date()
    start = datetime.combine(local, time(0), ZoneInfo(tz)).astimezone(UTC)
    return start, start + timedelta(days=1)


async def run_replay(
    cfg: AppConfig,
    store: HistoryStore,
    spec: ReplaySpec,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    book = cfg.book(spec.book_id)
    strategies = [make_strategy(s.id, dict(s.params)) for s in book.strategies if s.enabled]
    if not strategies:
        raise BacktestError(f"book {book.id} has no enabled strategies to replay")
    strategies = [s for s in strategies if book.market in s.markets]
    start, end = day_bounds(spec.day, book.trading_day_tz)
    rng = _RangeSpec(
        book_id=spec.book_id, exchange=spec.exchange, symbol=spec.symbol, base_tf=spec.base_tf, start=start, end=end
    )
    out = await run_engine(
        cfg,
        store,
        rng,
        strategies,
        True,
        None,
        spec.speed,
        (lambda f: progress(f, "replaying")) if progress else None,
        cancelled,
    )
    label = f"Replay of {start:%Y-%m-%d} ({book.trading_day_tz}) with {', '.join(s.id for s in strategies)}"
    return _report(
        "replay",
        cfg,
        book,
        [out],
        [(start, end)],
        MIN_TRADES_DEFAULT,
        label,
        {
            "strategies": [s.id for s in strategies],
            "signal_list": [
                jdict(
                    {
                        k: s[k]
                        for k in (
                            "id",
                            "ts",
                            "strategy_id",
                            "direction",
                            "entry_ref",
                            "stop_loss",
                            "target",
                            "setup",
                            "decision",
                            "executed",
                            "risk_rule_blocked",
                        )
                    }
                    | {"regime": s["regime"].get("label")}
                )
                for s in out.signals
            ][:500],
        },
    )
