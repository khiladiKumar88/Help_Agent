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


def fill_from(inst: Instrument, side: Side, ref: Decimal, kind: str, cfg: SlippageConfig) -> FillQuote:
    """Apply the book's slippage model to a given reference price, rounding adverse to the trader."""
    sign = 1 if side is Side.BUY else -1
    raw = ref * (1 + sign * cfg.bps / BPS) if cfg.model == "bps" else ref + sign * cfg.extra_ticks * inst.tick_size
    price = round_to_step(raw, inst.tick_size, "up" if side is Side.BUY else "down")
    if price <= ZERO:
        price = inst.tick_size
    return FillQuote(price=price, reference_price=ref, reference_kind=kind, slippage=abs(price - ref))


def market_fill(inst: Instrument, side: Side, tick: Tick, cfg: SlippageConfig) -> FillQuote:
    ref, kind = touch_price(tick, side, cfg)
    return fill_from(inst, side, ref, kind, cfg)


def stop_fill(inst: Instrument, side: Side, tick: Tick, trigger: Decimal, cfg: SlippageConfig) -> FillQuote:
    """Fill a triggered stop-market order honestly.

    A stop is an instruction to leave at a level. When the market trades THROUGH that level the
    order executes near it, plus the usual slippage — you do not receive the single worst print
    of the bar. Filling at the bar's extreme instead (which is what using the raw tick does in a
    replay) made every stop-out cost about 1.1R rather than 1R, while targets filled exactly at
    their level: a systematic, one-sided bias worth roughly -0.1R per trade.

    The one case that genuinely fills worse is a gap: if the first price seen in this bar is
    already beyond the stop, the market never offered the level and the gap price is the truth.
    """
    touch, kind = touch_price(tick, side, cfg)
    gapped_past = (touch > trigger) if side is Side.BUY else (touch < trigger)
    if tick.gap and gapped_past:
        return fill_from(inst, side, touch, f"gap_through_stop:{kind}", cfg)
    return fill_from(inst, side, trigger, "stop_trigger", cfg)


def estimate_entry_fill(
    inst: Instrument, side: Side, tick: Tick, cfg: SlippageConfig, limit_price: Decimal | None = None
) -> FillQuote:
    """The entry fill the order preview should expect, using the very same model as the real fill.

    Mirrors `PaperBroker._process_order`: a market order pays the slippage model on top of the
    touch price; a limit entry never fills worse than its limit. Preview and execution therefore
    cannot drift apart. It is still an estimate: the real fill happens on the NEXT tick.
    """
    q = market_fill(inst, side, tick, cfg)
    if limit_price is None:
        return q
    price = min(q.price, limit_price) if side is Side.BUY else max(q.price, limit_price)
    return FillQuote(
        price=price,
        reference_price=q.reference_price,
        reference_kind=q.reference_kind,
        slippage=abs(price - q.reference_price),
    )


def exit_trigger_price(tick: Tick, side: Side) -> Decimal:
    """Price used to decide whether a stop/target on the EXIT side has been touched."""
    if side is Side.SELL:
        return tick.bid if tick.bid is not None and tick.bid > 0 else tick.ltp
    return tick.ask if tick.ask is not None and tick.ask > 0 else tick.ltp
