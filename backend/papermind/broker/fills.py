"""Fill-price models. All rounding is adverse to the trader (buy up, sell down)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from papermind.core.config import SlippageConfig
from papermind.core.money import BPS, ZERO, round_to_step
from papermind.core.types import Instrument, Side, Tick


@dataclass(frozen=True)
class FillQuote:
    price: Decimal
    reference_price: Decimal
    reference_kind: str  # ask | bid | ltp_half_spread
    slippage: Decimal  # adverse difference between fill and reference (>= 0)


def touch_price(tick: Tick, side: Side, cfg: SlippageConfig) -> tuple[Decimal, str]:
    """Price we would hit: ask for a buy, bid for a sell; LTP +/- half the estimated spread otherwise."""
    if side is Side.BUY and tick.ask is not None and tick.ask > 0:
        return tick.ask, "ask"
    if side is Side.SELL and tick.bid is not None and tick.bid > 0:
        return tick.bid, "bid"
    half = tick.ltp * cfg.est_spread_bps / BPS / 2
    return (tick.ltp + half if side is Side.BUY else tick.ltp - half), "ltp_half_spread"


def market_fill(inst: Instrument, side: Side, tick: Tick, cfg: SlippageConfig) -> FillQuote:
    ref, kind = touch_price(tick, side, cfg)
    sign = 1 if side is Side.BUY else -1
    raw = ref * (1 + sign * cfg.bps / BPS) if cfg.model == "bps" else ref + sign * cfg.extra_ticks * inst.tick_size
    price = round_to_step(raw, inst.tick_size, "up" if side is Side.BUY else "down")
    if price <= ZERO:
        price = inst.tick_size
    return FillQuote(price=price, reference_price=ref, reference_kind=kind, slippage=abs(price - ref))


def exit_trigger_price(tick: Tick, side: Side) -> Decimal:
    """Price used to decide whether a stop/target on the EXIT side has been touched."""
    if side is Side.SELL:
        return tick.bid if tick.bid is not None and tick.bid > 0 else tick.ltp
    return tick.ask if tick.ask is not None and tick.ask > 0 else tick.ltp
