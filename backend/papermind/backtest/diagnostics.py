"""Where does the R go? A per-run breakdown of how each R-multiple was actually earned or lost.

The learning loop treats R as evidence, so "the strategy lost 0.6R per trade" is only useful if
we can say *which part of the machinery* took it. This module splits a run into:

  * R by exit reason (stop / target / trailing / square-off / end of test / ...)
  * the average winner and the average loser, so an asymmetric loser (worse than -1R) shows up
  * the slippage actually paid, per side, in bps and in R
  * fees in R
  * how far past the stop level a stop-out really filled ("overshoot") — the single most likely
    place for a backtest to flatter or punish itself
  * how often the stop and the target sat inside the SAME signal bar, where the bar data cannot
    say which came first and the engine has to assume an order

Everything is a pure function of trades, fills and candles, so it is testable without a market.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from papermind.core.money import BPS, ZERO
from papermind.core.types import Candle, Direction
from papermind.journal.entities import Trade


@dataclass(frozen=True)
class Bucket:
    reason: str
    trades: int
    avg_r: float | None
    total_net: Decimal
    share: float  # fraction of all closed trades

    def as_dict(self) -> dict[str, Any]:
        return {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class Diagnostics:
    trades: int
    avg_r: float | None
    by_exit_reason: list[Bucket]
    avg_winner_r: float | None
    avg_loser_r: float | None
    win_rate: float
    # costs, all expressed per trade
    fees_in_r: float | None
    funding_in_r: float | None
    entry_slippage_bps: float | None
    exit_slippage_bps: float | None
    entry_slippage_in_r: float | None
    exit_slippage_in_r: float | None
    # the honesty checks
    stop_overshoot_bps: float | None  # how far PAST the stop a stop-out filled
    stop_overshoot_in_r: float | None
    stop_exits: int
    both_levels_in_one_bar_pct: float | None
    bars_checked: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if isinstance(v, Decimal):
                out[k] = format(v, "f")
            elif k == "by_exit_reason":
                out[k] = [{bk: (format(bv, "f") if isinstance(bv, Decimal) else bv) for bk, bv in b.items()} for b in v]
            else:
                out[k] = v
        return out


def _avg(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _r(t: Trade) -> float | None:
    return float(t.r_multiple) if t.r_multiple is not None else None


def _risk_per_unit(t: Trade) -> Decimal | None:
    """Price distance that equals 1R for this trade (entry to initial stop)."""
    if t.avg_entry is None:
        return None
    d = abs(t.avg_entry - t.initial_sl)
    return d if d > 0 else None


def exit_reason_buckets(closed: Sequence[Trade]) -> list[Bucket]:
    groups: dict[str, list[Trade]] = {}
    for t in closed:
        groups.setdefault(str(t.exit_reason) if t.exit_reason else "none", []).append(t)
    out = []
    for reason, ts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rs = [r for r in (_r(t) for t in ts) if r is not None]
        out.append(
            Bucket(
                reason=reason,
                trades=len(ts),
                avg_r=_avg(rs),
                total_net=sum((t.net_pnl for t in ts), ZERO),
                share=round(len(ts) / len(closed), 4) if closed else 0.0,
            )
        )
    return out


def _slippage_by_side(
    closed: Sequence[Trade], fills: dict[str, list[dict[str, Any]]]
) -> tuple[list[float], list[float], list[float], list[float]]:
    """(entry bps, exit bps, entry R, exit R) for every fill we can attribute."""
    entry_bps: list[float] = []
    exit_bps: list[float] = []
    entry_r: list[float] = []
    exit_r: list[float] = []
    for t in closed:
        risk = _risk_per_unit(t)
        # fills come back ordered by time, so the first one is the entry and the rest are exits
        for i, f in enumerate(fills.get(t.id, [])):
            slip = f.get("slippage")
            ref = f.get("reference_price")
            if slip is None or not ref or Decimal(str(ref)) <= 0:
                continue
            slip_d = Decimal(str(slip))
            bps = float(slip_d / Decimal(str(ref)) * BPS)
            in_r = float(slip_d / risk) if risk else None
            bps_out, r_out = (entry_bps, entry_r) if i == 0 else (exit_bps, exit_r)
            bps_out.append(bps)
            if in_r is not None:
                r_out.append(in_r)
    return entry_bps, exit_bps, entry_r, exit_r


def _stop_overshoot(
    closed: Sequence[Trade], fills: dict[str, list[dict[str, Any]]]
) -> tuple[list[float], list[float], int]:
    """How far BEYOND the stop level each stop-out actually filled, in bps and in R.

    A stop-market order should fill at roughly the stop price plus the slippage model. If the sim
    fills it at the bar's extreme instead, this number blows up and every loser costs more than
    1R — which is exactly the bias this measures.
    """
    bps: list[float] = []
    in_r: list[float] = []
    n = 0
    for t in closed:
        if str(t.exit_reason) != "stop_loss" or t.avg_exit is None:
            continue
        n += 1
        stop = t.current_sl
        if stop <= 0:
            continue
        # adverse direction: a long exits below its stop, a short above it
        past = (stop - t.avg_exit) if t.direction is Direction.LONG else (t.avg_exit - stop)
        bps.append(float(past / stop * BPS))
        risk = _risk_per_unit(t)
        if risk:
            in_r.append(float(past / risk))
    return bps, in_r, n


def _both_levels_in_one_bar(closed: Sequence[Trade], candles: Sequence[Candle]) -> tuple[float | None, int]:
    """Share of trades whose stop AND target both sat inside a single bar they were open for.

    When that happens the OHLC cannot say which was touched first, so the engine's assumed
    intrabar path decides the outcome — the result is an assumption, not data.
    """
    if not candles:
        return None, 0
    by_time = sorted(candles, key=lambda c: c.ts_open)
    hits = 0
    checked = 0
    for t in closed:
        if t.target is None or t.opened_at is None or t.closed_at is None:
            continue
        checked += 1
        lo = min(t.current_sl, t.target)
        hi = max(t.current_sl, t.target)
        for c in by_time:
            if c.ts_open < t.opened_at or c.ts_open > t.closed_at:
                continue
            if c.low <= lo and c.high >= hi:
                hits += 1
                break
    return (round(hits / checked, 4) if checked else None), checked


def run_diagnostics(
    trades: Sequence[Trade],
    fills: dict[str, list[dict[str, Any]]] | None = None,
    candles: Sequence[Candle] | None = None,
) -> Diagnostics:
    """Break one actor's closed trades down into where the R came from."""
    closed = [t for t in trades if t.closed_at is not None]
    fills = fills or {}
    rs = [r for r in (_r(t) for t in closed) if r is not None]
    winners = [r for r in rs if r > 0]
    losers = [r for r in rs if r <= 0]

    fee_r: list[float] = []
    fund_r: list[float] = []
    for t in closed:
        if t.initial_risk and t.initial_risk > 0:
            fee_r.append(float(t.charges / t.initial_risk))
            fund_r.append(float(-t.funding / t.initial_risk))

    entry_bps, exit_bps, entry_r, exit_r = _slippage_by_side(closed, fills)
    over_bps, over_r, stop_exits = _stop_overshoot(closed, fills)
    both_pct, bars_checked = _both_levels_in_one_bar(closed, candles or [])

    notes: list[str] = []
    avg_loser = _avg(losers)
    if avg_loser is not None and avg_loser < -1.15:
        notes.append(
            f"The average loser is {avg_loser:+.2f}R, worse than the -1R a stop should cost. "
            "Look at stop_overshoot_in_r: that is the part the stop fill is giving away."
        )
    if both_pct is not None and both_pct > 0.2:
        notes.append(
            f"{both_pct:.0%} of trades had the stop and target inside one bar, so their outcome "
            "came from the assumed intrabar path rather than from data. Use a finer base "
            "timeframe for fills."
        )
    return Diagnostics(
        trades=len(closed),
        avg_r=_avg(rs),
        by_exit_reason=exit_reason_buckets(closed),
        avg_winner_r=_avg(winners),
        avg_loser_r=avg_loser,
        win_rate=round(len(winners) / len(rs), 4) if rs else 0.0,
        fees_in_r=_avg(fee_r),
        funding_in_r=_avg(fund_r),
        entry_slippage_bps=_avg(entry_bps),
        exit_slippage_bps=_avg(exit_bps),
        entry_slippage_in_r=_avg(entry_r),
        exit_slippage_in_r=_avg(exit_r),
        stop_overshoot_bps=_avg(over_bps),
        stop_overshoot_in_r=_avg(over_r),
        stop_exits=stop_exits,
        both_levels_in_one_bar_pct=both_pct,
        bars_checked=bars_checked,
        notes=notes,
    )
