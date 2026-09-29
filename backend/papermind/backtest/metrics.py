"""Performance metrics. Pure functions over closed trades / equity points. All P&L is after charges."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from papermind.core.money import ZERO
from papermind.journal.entities import Trade


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate: float  # 0..1
    net_profit: Decimal  # after all fees + funding
    gross_profit: Decimal  # sum of winning net P&L
    gross_loss: Decimal  # sum of losing net P&L (<= 0)
    fees: Decimal
    funding: Decimal
    avg_r: float | None  # expectancy in R
    profit_factor: float | None  # gross profit / |gross loss|; None if no losses
    max_drawdown: Decimal  # peak-to-trough of cumulative net P&L (>= 0)
    max_drawdown_pct: float  # vs starting capital + running peak
    return_pct: float
    sharpe: float | None  # annualized, from per-trade-day returns
    best_trade: Decimal
    worst_trade: Decimal
    avg_hold_minutes: float | None

    def as_dict(self) -> dict[str, Any]:
        return {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def _drawdown(pnls: Sequence[Decimal], capital: Decimal) -> tuple[Decimal, float]:
    equity = capital
    peak = capital
    max_dd = ZERO
    max_dd_pct = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            max_dd_pct = max(max_dd_pct, float(dd / peak) * 100.0)
    return max_dd, round(max_dd_pct, 3)


def _sharpe(trades: Sequence[Trade], capital: Decimal, periods_per_year: float) -> float | None:
    by_day: dict[Any, float] = {}
    for t in trades:
        if t.closed_at is None:
            continue
        d = t.closed_at.date()
        by_day[d] = by_day.get(d, 0.0) + float(t.net_pnl / capital)
    if len(by_day) < 5:
        return None
    first, last = min(by_day), max(by_day)
    days = (last - first).days + 1
    rets = [by_day.get(first.fromordinal(first.toordinal() + i), 0.0) for i in range(days)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    return round(mean / math.sqrt(var) * math.sqrt(periods_per_year), 3)


def trade_metrics(trades: Sequence[Trade], capital: Decimal, periods_per_year: float = 365.0) -> Metrics:
    closed = sorted((t for t in trades if t.closed_at is not None), key=lambda t: t.closed_at)  # type: ignore[arg-type, return-value]
    pnls = [t.net_pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp = sum(wins, ZERO)
    gl = sum(losses, ZERO)
    rs = [float(t.r_multiple) for t in closed if t.r_multiple is not None]
    holds = [(t.closed_at - t.opened_at).total_seconds() / 60 for t in closed if t.opened_at and t.closed_at]
    dd, dd_pct = _drawdown(pnls, capital)
    net = sum(pnls, ZERO)
    return Metrics(
        trades=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / len(closed), 4) if closed else 0.0,
        net_profit=net,
        gross_profit=gp,
        gross_loss=gl,
        fees=sum((t.charges for t in closed), ZERO),
        funding=sum((t.funding for t in closed), ZERO),
        avg_r=round(sum(rs) / len(rs), 4) if rs else None,
        profit_factor=round(float(gp / -gl), 3) if gl < 0 else None,
        max_drawdown=dd,
        max_drawdown_pct=dd_pct,
        return_pct=round(float(net / capital) * 100.0, 3) if capital else 0.0,
        sharpe=_sharpe(closed, capital, periods_per_year),
        best_trade=max(pnls) if pnls else ZERO,
        worst_trade=min(pnls) if pnls else ZERO,
        avg_hold_minutes=round(sum(holds) / len(holds), 1) if holds else None,
    )


def cumulative_series(trades: Sequence[Trade]) -> list[dict[str, Any]]:
    """[{t, v}] cumulative net P&L at each trade close (for charts)."""
    out = []
    total = ZERO
    for t in sorted((t for t in trades if t.closed_at), key=lambda t: t.closed_at):  # type: ignore[arg-type, return-value]
        total += t.net_pnl
        out.append({"t": t.closed_at.isoformat() if t.closed_at else None, "v": format(total, "f")})
    return out


@dataclass(frozen=True)
class BuyHold:
    start: datetime
    end: datetime
    entry: Decimal
    exit: Decimal
    qty: Decimal
    fees: Decimal
    net_profit: Decimal
    return_pct: float
    max_drawdown_pct: float

    def as_dict(self) -> dict[str, Any]:
        return {
            k: (format(v, "f") if isinstance(v, Decimal) else v.isoformat() if isinstance(v, datetime) else v)
            for k, v in asdict(self).items()
        }
