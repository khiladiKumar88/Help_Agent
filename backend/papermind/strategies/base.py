"""Strategy plugin interface.

A strategy looks at CLOSED candles (via a FeatureFrame) and may emit one SignalIntent with
entry reference, stop-loss, target and a plain-English setup description. Strategies never
size positions or touch the broker: sizing and every risk rule live in risk/.

Anti-overfitting: every strategy declares a SMALL parameter grid (<= MAX_GRID combinations);
the walk-forward optimizer refuses anything larger.
"""

from __future__ import annotations

import itertools
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from papermind.core.config import BookConfig, Market
from papermind.core.money import D, round_to_step
from papermind.core.types import Direction, Instrument
from papermind.regime.detector import Regime
from papermind.strategies.features import FeatureFrame

MAX_GRID = 16
ParamValue = int | float


@dataclass(frozen=True)
class StrategyContext:
    now: datetime  # candle close time == current clock time
    book: BookConfig
    instrument: Instrument
    timeframe: str
    frame: FeatureFrame
    regime: Regime
    funding_rate: Decimal | None = None


@dataclass(frozen=True)
class SignalIntent:
    direction: Direction
    entry_ref: Decimal
    stop_loss: Decimal
    target: Decimal | None
    setup: str
    tags: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    markets: ClassVar[frozenset[Market]] = frozenset({Market.CRYPTO, Market.INDIA})
    default_params: ClassVar[dict[str, ParamValue]]
    param_grid: ClassVar[dict[str, list[ParamValue]]]

    def __init__(self, params: dict[str, ParamValue] | None = None) -> None:
        unknown = set(params or {}) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.id}: unknown params {sorted(unknown)}")
        self.params: dict[str, ParamValue] = {**self.default_params, **(params or {})}

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        if getattr(cls, "param_grid", None) is not None and grid_size(cls.param_grid) > MAX_GRID:
            raise ValueError(f"{cls.__name__}: param grid too large (max {MAX_GRID} combinations)")

    @property
    def warmup_bars(self) -> int:
        return 60

    def reset(self) -> None:  # noqa: B027  (optional hook)
        """Clear per-run state (e.g. one-trade-per-day memory)."""

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None: ...

    # ------------------------------------------------------------------ helpers
    def p(self, key: str) -> float:
        return float(self.params[key])

    def pi(self, key: str) -> int:
        return int(self.params[key])

    @staticmethod
    def build(
        ctx: StrategyContext,
        direction: Direction,
        stop: float,
        rr: float | None,
        setup: str,
        target: float | None = None,
        **tags: Any,
    ) -> SignalIntent | None:
        """Round levels to the tick grid (stop rounded AWAY from entry) and sanity-check them."""
        tick = ctx.instrument.tick_size
        entry = D(ctx.frame.last(ctx.frame.close))
        if math.isnan(stop) or stop <= 0:
            return None
        sign = direction.sign
        sl = round_to_step(D(round(stop, 10)), tick, "down" if sign > 0 else "up")
        risk = (entry - sl) * sign
        if risk <= tick:
            return None
        tgt: Decimal | None
        if target is not None:
            if math.isnan(target):
                return None
            tgt = round_to_step(D(round(target, 10)), tick)
        elif rr is not None:
            tgt = round_to_step(entry + sign * risk * D(rr), tick)
        else:
            tgt = None
        if tgt is not None and (tgt - entry) * sign <= 0:
            return None
        return SignalIntent(direction, entry, sl, tgt, setup, dict(tags))


def grid_size(grid: dict[str, list[ParamValue]]) -> int:
    n = 1
    for v in grid.values():
        n *= max(1, len(v))
    return n


def expand_grid(grid: dict[str, list[ParamValue]]) -> list[dict[str, ParamValue]]:
    if grid_size(grid) > MAX_GRID:
        raise ValueError(f"param grid too large (max {MAX_GRID})")
    keys = sorted(grid)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(grid[k] for k in keys))]
