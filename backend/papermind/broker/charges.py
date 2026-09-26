"""Trading charges. Rates come from config/charges/*.yaml (each file carries source_url,
as_of and verified — shown in Settings). Nothing here hardcodes a rate."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from papermind.core.config import ChargesConfig, CryptoChargesConfig, IndiaFnoChargesConfig
from papermind.core.money import PCT, ZERO, money
from papermind.core.types import ChargeBreakdown, Instrument, Liquidity, Side

CRORE = Decimal("10000000")


class ChargesModel(Protocol):
    def compute(
        self, inst: Instrument, side: Side, qty: Decimal, price: Decimal, liquidity: Liquidity
    ) -> ChargeBreakdown: ...

    def round_trip(self, inst: Instrument, entry_side: Side, qty: Decimal, entry: Decimal, exit_: Decimal) -> Decimal:
        """Conservative estimate: taker on both legs."""
        ...


def turnover(inst: Instrument, qty: Decimal, price: Decimal) -> Decimal:
    return qty * inst.contract_size * price


class CryptoCharges:
    def __init__(self, cfg: CryptoChargesConfig) -> None:
        self.cfg = cfg

    def compute(
        self, inst: Instrument, side: Side, qty: Decimal, price: Decimal, liquidity: Liquidity
    ) -> ChargeBreakdown:
        pct = self.cfg.maker_pct if liquidity is Liquidity.MAKER else self.cfg.taker_pct
        fee = money(turnover(inst, qty, price) * pct / PCT)
        gst = money(fee * self.cfg.gst_pct / PCT)
        return ChargeBreakdown(exchange_fee=fee, gst=gst)

    def round_trip(self, inst: Instrument, entry_side: Side, qty: Decimal, entry: Decimal, exit_: Decimal) -> Decimal:
        a = self.compute(inst, entry_side, qty, entry, Liquidity.TAKER)
        b = self.compute(inst, entry_side.opposite, qty, exit_, Liquidity.TAKER)
        return a.total + b.total


class IndiaFnoCharges:
    """NSE F&O charges (per executed order). Options: turnover = premium x qty."""

    PLACES = 2

    def __init__(self, cfg: IndiaFnoChargesConfig) -> None:
        self.cfg = cfg

    def compute(
        self, inst: Instrument, side: Side, qty: Decimal, price: Decimal, liquidity: Liquidity
    ) -> ChargeBreakdown:
        c = self.cfg
        to = turnover(inst, qty, price)
        is_opt = inst.is_option
        brokerage = c.brokerage_per_order
        if c.brokerage_pct_cap is not None:
            brokerage = min(brokerage, to * c.brokerage_pct_cap / PCT)
        stt = ZERO
        if side is Side.SELL:
            stt = to * (c.stt_options_sell_pct if is_opt else c.stt_futures_sell_pct) / PCT
        exch = to * (c.exchange_options_pct if is_opt else c.exchange_futures_pct) / PCT
        sebi = to * c.sebi_per_crore / CRORE
        stamp = ZERO
        if side is Side.BUY:
            stamp = to * (c.stamp_options_buy_pct if is_opt else c.stamp_futures_buy_pct) / PCT
        ipft = to * c.ipft_pct / PCT
        gst = (brokerage + exch + sebi + ipft) * c.gst_pct / PCT
        p = self.PLACES
        return ChargeBreakdown(
            brokerage=money(brokerage, p),
            stt=money(stt, p),
            exchange_txn=money(exch, p),
            sebi=money(sebi, p),
            stamp=money(stamp, p),
            ipft=money(ipft, p),
            gst=money(gst, p),
        )

    def round_trip(self, inst: Instrument, entry_side: Side, qty: Decimal, entry: Decimal, exit_: Decimal) -> Decimal:
        a = self.compute(inst, entry_side, qty, entry, Liquidity.TAKER)
        b = self.compute(inst, entry_side.opposite, qty, exit_, Liquidity.TAKER)
        return a.total + b.total


def make_charges(cfg: ChargesConfig) -> CryptoCharges | IndiaFnoCharges:
    if isinstance(cfg, CryptoChargesConfig):
        return CryptoCharges(cfg)
    return IndiaFnoCharges(cfg)
