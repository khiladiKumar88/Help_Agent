from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from papermind.api.deps import get_engine
from papermind.core.serde import jsonable
from papermind.engine import Engine

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/instruments")
def instruments(book_id: str | None = None, engine: Engine = Depends(get_engine)) -> list[dict[str, Any]]:
    insts = engine.registry.all()
    if book_id:
        book = engine.cfg.book(book_id)
        insts = [i for i in insts if i.symbol in book.instruments and i.segment is book.segment]
    return [jsonable(i) for i in insts]


@router.get("/quote")
def quote(instrument_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    if instrument_id not in engine.registry:
        raise HTTPException(404, "unknown instrument")
    tick = engine.market.last(instrument_id)
    return {
        "tick": jsonable(tick) if tick else None,
        "stale": engine.broker.is_stale(instrument_id),
        "age_seconds": engine.market.age_seconds(instrument_id, engine.clock.now()),
    }


@router.get("/candles")
def candles(
    instrument_id: str, tf: str = "1m", limit: int = 300, engine: Engine = Depends(get_engine)
) -> dict[str, Any]:
    if instrument_id not in engine.registry:
        raise HTTPException(404, "unknown instrument")
    closed = engine.builder.closed(instrument_id, tf, min(limit, 2000))
    forming = engine.builder.forming(instrument_id) if tf == "1m" else None
    return {"closed": jsonable(closed), "forming": jsonable(forming) if forming else None}
