"""Synthetic option pricing for backtests (APPROXIMATE).

Expired Indian index options have no free price history, so option backtests (Phase 5) price
contracts from the underlying with Black-Scholes:
  - volatility: India VIX (annualized %, /100) when available, else realized volatility of the
    underlying over a lookback, times a configurable IV/RV multiplier, clamped to [floor, cap];
  - a configurable bid/ask spread (% of premium, at least N ticks), so fills pay the spread;
  - premiums are rounded to the option tick and never go below one tick.
Anything produced here is flagged approximate=True and every report says so.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from papermind.core.events import EventBus, Topic
from papermind.core.money import D, round_to_step
from papermind.core.types import Instrument, InstrumentKind, Tick

SECONDS_PER_YEAR = 365.0 * 24 * 3600
APPROXIMATE_NOTE = (
    "Option prices are synthetic: Black-Scholes on the underlying with IV from India VIX or realized volatility "
    "and a modelled spread. Real historical option quotes were not available, so treat results as approximate."
)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float  # per 1.00 (100 vol points)
    theta: float  # per year


def black_scholes(
    spot: float, strike: float, t_years: float, rate: float, vol: float, call: bool, div_yield: float = 0.0
) -> Greeks:
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if t_years <= 0 or vol <= 0:
        intrinsic = max(0.0, spot - strike) if call else max(0.0, strike - spot)
        delta = (1.0 if spot > strike else 0.0) if call else (-1.0 if spot < strike else 0.0)
        return Greeks(intrinsic, delta, 0.0, 0.0, 0.0)
    sq = vol * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div_yield + 0.5 * vol * vol) * t_years) / sq
    d2 = d1 - sq
    df_r, df_q = math.exp(-rate * t_years), math.exp(-div_yield * t_years)
    if call:
        price = spot * df_q * norm_cdf(d1) - strike * df_r * norm_cdf(d2)
        delta = df_q * norm_cdf(d1)
        theta = (
            -spot * df_q * norm_pdf(d1) * vol / (2 * math.sqrt(t_years))
            - rate * strike * df_r * norm_cdf(d2)
            + div_yield * spot * df_q * norm_cdf(d1)
        )
    else:
        price = strike * df_r * norm_cdf(-d2) - spot * df_q * norm_cdf(-d1)
        delta = df_q * (norm_cdf(d1) - 1.0)
        theta = (
            -spot * df_q * norm_pdf(d1) * vol / (2 * math.sqrt(t_years))
            + rate * strike * df_r * norm_cdf(-d2)
            - div_yield * spot * df_q * norm_cdf(-d1)
        )
    gamma = df_q * norm_pdf(d1) / (spot * sq)
    vega = spot * df_q * norm_pdf(d1) * math.sqrt(t_years)
    return Greeks(max(price, 0.0), delta, gamma, vega, theta)


class SyntheticOptionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    risk_free_rate: float = 0.065  # India ~ T-bill; configurable
    iv_multiplier: float = 1.1  # implied vol is usually above realized
    vol_floor: float = 0.08
    vol_cap: float = 1.5
    spread_pct: float = 1.0  # % of premium
    min_spread_ticks: int = 2


def volatility_from(vix: float | None, realized: float | None, cfg: SyntheticOptionConfig) -> float:
    """India VIX (e.g. 14.5 -> 0.145) if available, else realized vol x multiplier; clamped."""
    if vix is not None and vix > 0:
        vol = vix / 100.0
    elif realized is not None and realized > 0 and not math.isnan(realized):
        vol = realized * cfg.iv_multiplier
    else:
        vol = cfg.vol_floor
    return min(max(vol, cfg.vol_floor), cfg.vol_cap)


@dataclass(frozen=True)
class OptionQuote:
    bid: Decimal
    ask: Decimal
    mid: Decimal
    delta: float
    vol: float
    approximate: bool = True


def synthetic_quote(
    inst: Instrument, spot: Decimal, now: datetime, vol: float, cfg: SyntheticOptionConfig
) -> OptionQuote:
    if not inst.is_option or inst.strike is None or inst.expiry is None:
        raise ValueError(f"{inst.id} is not an option with strike and expiry")
    t = max(0.0, (inst.expiry - now).total_seconds() / SECONDS_PER_YEAR)
    g = black_scholes(float(spot), float(inst.strike), t, cfg.risk_free_rate, vol, inst.kind is InstrumentKind.CE)
    tick = inst.tick_size
    mid = max(round_to_step(D(round(g.price, 6)), tick), tick)
    half = max(mid * D(cfg.spread_pct) / 200, tick * cfg.min_spread_ticks / 2)
    bid = max(round_to_step(mid - half, tick, "down"), tick)
    ask = max(round_to_step(mid + half, tick, "up"), bid + tick)
    return OptionQuote(bid, ask, mid, g.delta, vol)


class SyntheticOptionFeed:
    """Emits option ticks priced from underlying ticks, so the PaperBroker trades options unchanged."""

    def __init__(
        self,
        bus: EventBus,
        publish_tick: Callable[[Tick], object],
        options: list[Instrument],
        underlying_id: str,
        vol_fn: Callable[[datetime], float],
        cfg: SyntheticOptionConfig | None = None,
    ) -> None:
        self.options = options
        self.underlying_id = underlying_id
        self.vol_fn = vol_fn
        self.cfg = cfg or SyntheticOptionConfig()
        self.publish_tick = publish_tick
        self.quotes_emitted = 0
        bus.subscribe(Topic.TICK, self.on_tick)

    async def on_tick(self, tick: Tick) -> None:
        if tick.instrument_id != self.underlying_id:
            return
        vol = self.vol_fn(tick.ts)
        for inst in self.options:
            if inst.expiry is not None and tick.ts >= inst.expiry:
                continue
            q = synthetic_quote(inst, tick.ltp, tick.ts, vol, self.cfg)
            res = self.publish_tick(
                Tick(instrument_id=inst.id, ts=tick.ts, ltp=q.mid, bid=q.bid, ask=q.ask, source="synthetic_bs")
            )
            if inspect.isawaitable(res):
                await res
            self.quotes_emitted += 1
