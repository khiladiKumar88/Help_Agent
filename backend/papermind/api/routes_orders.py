from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from papermind.api.deps import get_engine, require_book
from papermind.broker.paper_broker import BrokerError
from papermind.core.serde import jsonable
from papermind.core.types import Actor, Direction, OrderRequest, OrderType, TrailingConfig
from papermind.engine import Engine

router = APIRouter(prefix="/api", tags=["orders"])


class ManualOrder(BaseModel):
    """A paper order placed by the human from the UI. Actor is always 'human' here."""

    book_id: str
    instrument_id: str
    direction: Direction
    qty: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    trailing: TrailingConfig | None = None
    leverage: Decimal = Decimal("1")
    note: str = ""

    def to_request(self) -> OrderRequest:
        return OrderRequest(actor=Actor.HUMAN, **self.model_dump())


@router.post("/orders/preview")
def preview(body: ManualOrder, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    require_book(engine, body.book_id)
    return engine.broker.preview(body.to_request())


@router.post("/orders")
async def place(body: ManualOrder, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    require_book(engine, body.book_id)
    decision, trade = await engine.broker.submit(body.to_request())
    return {
        "approved": decision.approved,
        "decision": jsonable(decision),
        "trade": engine.broker.trade_view(trade) if trade else None,
    }


class ModifyBody(BaseModel):
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    clear_target: bool = False


@router.patch("/trades/{trade_id}")
async def modify(trade_id: str, body: ModifyBody, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    try:
        t = await engine.broker.modify(trade_id, body.stop_loss, body.target, body.clear_target)
    except BrokerError as exc:
        raise HTTPException(422, {"message": str(exc), "rule_id": exc.rule_id}) from exc
    return engine.broker.trade_view(t)


@router.post("/trades/{trade_id}/close")
async def close(trade_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    try:
        t = await engine.broker.close(trade_id)
    except BrokerError as exc:
        raise HTTPException(422, {"message": str(exc)}) from exc
    return engine.broker.trade_view(t)


@router.post("/trades/{trade_id}/cancel")
async def cancel(trade_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    try:
        t = await engine.broker.cancel(trade_id)
    except BrokerError as exc:
        raise HTTPException(422, {"message": str(exc)}) from exc
    return engine.broker.trade_view(t)


@router.get("/trades/{trade_id}")
def trade_detail(trade_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    t = engine.journal.get_trade(trade_id)
    if t is None:
        raise HTTPException(404, "trade not found")
    return {**engine.broker.trade_view(t), "fills": jsonable(engine.journal.fills_for_trade(trade_id))}
