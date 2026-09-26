from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from papermind.api.deps import get_engine, require_book
from papermind.core.config import Mode
from papermind.core.serde import jdict, jlist, jsonable
from papermind.core.types import Account
from papermind.engine import Engine

router = APIRouter(prefix="/api/books", tags=["books"])


@router.get("")
def list_books(engine: Engine = Depends(get_engine)) -> list[dict[str, Any]]:
    out = []
    for b in engine.cfg.books.values():
        out.append(
            jsonable(
                {
                    "id": b.id,
                    "market": b.market,
                    "segment": b.segment,
                    "style": b.style,
                    "currency": b.currency,
                    "mode": engine.broker.mode(b.id),
                    "enabled": b.enabled,
                    "status": engine.agent_status(b.id),
                }
            )
        )
    return out


@router.get("/{book_id}")
def book_summary(book_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    require_book(engine, book_id)
    return engine.broker.book_summary(book_id)


@router.get("/{book_id}/config")
def book_config(book_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    require_book(engine, book_id)
    b = engine.cfg.book(book_id)
    charges = engine.cfg.charges[b.charges]
    return jdict({"book": b.model_dump(), "charges": {"name": b.charges, **charges.model_dump()}})


class ModeBody(BaseModel):
    mode: Mode


@router.put("/{book_id}/mode")
def set_mode(book_id: str, body: ModeBody, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    require_book(engine, book_id)
    if body.mode is not Mode.MANUAL:
        # Co-pilot / Auto need the scanner + analyst (Phases 2-3). Refuse rather than pretend.
        raise HTTPException(409, f"mode '{body.mode}' becomes available in a later phase; only manual works now")
    engine.broker.set_mode(book_id, str(body.mode))
    return {"book_id": book_id, "mode": body.mode}


@router.get("/{book_id}/trades")
def trades(
    book_id: str,
    status: str | None = None,
    actor: str | None = None,
    limit: int = 200,
    engine: Engine = Depends(get_engine),
) -> list[dict[str, Any]]:
    require_book(engine, book_id)
    return [
        engine.broker.trade_view(t)
        for t in engine.journal.list_trades(book_id, Account.MAIN, status=status, actor=actor, limit=min(limit, 1000))
    ]


@router.get("/{book_id}/equity")
def equity(book_id: str, since: datetime | None = None, engine: Engine = Depends(get_engine)) -> list[dict[str, Any]]:
    require_book(engine, book_id)
    return jlist(engine.journal.equity_curve(book_id, Account.MAIN, since))


@router.get("/{book_id}/risk-decisions")
def risk_decisions(
    book_id: str, rejected_only: bool = False, limit: int = 50, engine: Engine = Depends(get_engine)
) -> list[dict[str, Any]]:
    require_book(engine, book_id)
    return jlist(engine.journal.list_risk_decisions(book_id, min(limit, 500), rejected_only))


@router.get("/{book_id}/actor-pnl")
def actor_pnl(book_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    """Cumulative net P&L of closed trades per actor (You vs Agent vs Baseline)."""
    require_book(engine, book_id)
    series: dict[str, list[dict[str, Any]]] = {"human": [], "agent": [], "baseline": []}
    closed = [t for t in engine.journal.list_trades(book_id, status="closed", limit=5000) if t.closed_at]
    closed.sort(key=lambda t: t.closed_at)  # type: ignore[arg-type, return-value]
    totals: dict[str, Any] = {}
    for t in closed:
        a = str(t.actor)
        totals[a] = totals.get(a, 0) + t.net_pnl
        series.setdefault(a, []).append({"ts": t.closed_at, "cum_net_pnl": totals[a], "r": t.r_multiple})
    return jdict({"series": series, "totals": totals})
