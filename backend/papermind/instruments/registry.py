"""Instrument registry. Contract specs always come from the exchange / broker instrument
master (ccxt markets, Angel One scrip master) — never hardcoded."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from papermind.core.config import Market, Segment
from papermind.core.money import D, D_or_none
from papermind.core.types import Instrument, InstrumentKind
from papermind.db.base import Database
from papermind.db.models import InstrumentRow

log = logging.getLogger(__name__)

CCXT_TICK_SIZE = 4
CCXT_DECIMAL_PLACES = 2


def crypto_instrument_id(exchange: str, symbol: str) -> str:
    return f"crypto:{exchange}:{symbol}"


def _precision_to_step(value: Any, precision_mode: int) -> Decimal:
    if value is None:
        raise ValueError("missing precision")
    if precision_mode == CCXT_TICK_SIZE:
        return D(value)
    if precision_mode == CCXT_DECIMAL_PLACES:
        return Decimal(1).scaleb(-int(value))
    raise ValueError(f"unsupported ccxt precision mode {precision_mode}")


def instrument_from_ccxt_market(exchange: str, m: dict[str, Any], precision_mode: int) -> Instrument | None:
    """Map a ccxt unified market to an Instrument. Returns None for unsupported markets."""
    if m.get("inverse"):
        return None  # coin-margined P&L maths not supported; USDT-settled books only
    mtype = m.get("type")
    kind: InstrumentKind
    segment: Segment
    if mtype == "spot":
        kind, segment = InstrumentKind.SPOT, Segment.SPOT
    elif mtype == "swap":
        kind, segment = InstrumentKind.PERP, Segment.FUTURES
    elif mtype == "future":
        kind, segment = InstrumentKind.FUT, Segment.FUTURES
    elif mtype == "option":
        kind = InstrumentKind.CE if m.get("optionType") == "call" else InstrumentKind.PE
        segment = Segment.OPTIONS
    else:
        return None
    precision = m.get("precision") or {}
    tick = _precision_to_step(precision.get("price"), precision_mode)
    step = _precision_to_step(precision.get("amount"), precision_mode)
    min_qty = D_or_none(((m.get("limits") or {}).get("amount") or {}).get("min")) or step
    expiry_ms = m.get("expiry")
    return Instrument(
        id=crypto_instrument_id(exchange, m["symbol"]),
        market=Market.CRYPTO,
        segment=segment,
        exchange=exchange,
        symbol=m["symbol"],
        underlying=m["base"],
        kind=kind,
        quote_ccy=m["quote"],
        settle_ccy=m.get("settle") or m["quote"],
        contract_size=D(m.get("contractSize") or 1),
        lot_size=Decimal(1),
        tick_size=tick,
        qty_step=step,
        min_qty=min_qty,
        expiry=datetime.fromtimestamp(expiry_ms / 1000, UTC) if expiry_ms else None,
        strike=D_or_none(m.get("strike")),
        broker_token=str(m.get("id")) if m.get("id") is not None else None,
        active=bool(m.get("active", True)),
    )


class InstrumentRegistry:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._by_id: dict[str, Instrument] = {}

    def __contains__(self, instrument_id: str) -> bool:
        return instrument_id in self._by_id

    def get(self, instrument_id: str) -> Instrument:
        try:
            return self._by_id[instrument_id]
        except KeyError as exc:
            raise KeyError(f"unknown instrument '{instrument_id}'") from exc

    def all(self) -> list[Instrument]:
        return list(self._by_id.values())

    def upsert(self, instruments: list[Instrument], now: datetime) -> None:
        for inst in instruments:
            self._by_id[inst.id] = inst
        if self._db is None:
            return
        with self._db.session() as s:
            for inst in instruments:
                s.merge(InstrumentRow(**inst.model_dump(), refreshed_at=now))

    def load_from_db(self) -> int:
        if self._db is None:
            return 0
        with self._db.session() as s:
            rows = s.query(InstrumentRow).all()
            for r in rows:
                data = {c: getattr(r, c) for c in Instrument.model_fields}
                self._by_id[r.id] = Instrument.model_validate(data)
        return len(rows)

    def load_ccxt_markets(
        self, exchange: str, markets: dict[str, dict[str, Any]], precision_mode: int, symbols: list[str], now: datetime
    ) -> list[Instrument]:
        out: list[Instrument] = []
        for sym in symbols:
            m = markets.get(sym)
            if m is None:
                log.warning("symbol not listed on exchange", extra={"exchange": exchange, "symbol": sym})
                continue
            inst = instrument_from_ccxt_market(exchange, m, precision_mode)
            if inst is None:
                log.warning("unsupported market type", extra={"exchange": exchange, "symbol": sym})
                continue
            out.append(inst)
        self.upsert(out, now)
        return out
