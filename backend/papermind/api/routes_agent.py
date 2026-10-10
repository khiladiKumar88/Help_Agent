"""Phase 3: co-pilot cards, signal detail with the analyst's reasoning, LLM status and settings.

Nothing here can place an order outside the normal path: approving a card goes through
`AgentExecutor.resolve_card` -> `place_sized` -> `broker.submit` -> the risk rules.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from papermind.api.deps import get_engine, require_book
from papermind.core.serde import jdict, jlist
from papermind.engine import Engine
from papermind.scanner.agent import CardError

router = APIRouter(prefix="/api", tags=["agent"])


# ------------------------------------------------------------------ co-pilot cards


@router.get("/books/{book_id}/cards")
def cards(book_id: str, engine: Engine = Depends(get_engine)) -> list[dict[str, Any]]:
    """Signal cards awaiting your Approve/Reject, oldest first."""
    require_book(engine, book_id)
    now = engine.clock.now()
    out = []
    for row in engine.journal.pending_cards(book_id):
        expires = row.get("card_expires_at")
        out.append(
            {
                **row,
                "seconds_left": max(0.0, (expires - now).total_seconds()) if expires else None,
                "expired": bool(expires and now > expires),
            }
        )
    return jlist(out)


class CardVerdict(BaseModel):
    approve: bool


@router.post("/signals/{signal_id}/card")
async def resolve_card(signal_id: str, body: CardVerdict, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    """Approve (place the trade) or reject (shadow-track what it would have done)."""
    try:
        return jdict(await engine.executor.resolve_card(signal_id, body.approve))
    except CardError as exc:
        raise HTTPException(422, {"message": str(exc)}) from exc


# ------------------------------------------------------------------ signals / journal


@router.get("/books/{book_id}/journal")
def journal_signals(
    book_id: str,
    decision: str | None = None,
    strategy_id: str | None = None,
    card_status: str | None = None,
    executed: bool | None = None,
    limit: int = 100,
    engine: Engine = Depends(get_engine),
) -> list[dict[str, Any]]:
    """Every signal — taken AND skipped — for the Journal page."""
    require_book(engine, book_id)
    return jlist(
        engine.journal.list_signals_filtered(
            book_id,
            limit=min(limit, 1000),
            decision=decision,
            strategy_id=strategy_id,
            card_status=card_status,
            executed=executed,
        )
    )


@router.get("/signals/{signal_id}")
def signal_detail(signal_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    """Full snapshot, the analyst's reasoning, the LLM call, and the outcome of every shadow."""
    row = engine.journal.get_signal(signal_id)
    if row is None:
        raise HTTPException(404, "signal not found")
    out: dict[str, Any] = {"signal": row}
    call_id = row.get("llm_call_id")
    out["llm_call"] = engine.journal.get_llm_call(call_id) if call_id else None
    for key, field in (
        ("trade", "trade_id"),
        ("baseline_trade", "baseline_trade_id"),
        ("raw_trade", "raw_trade_id"),
        ("rejected_trade", "rejected_trade_id"),
    ):
        tid = row.get(field)
        trade = engine.journal.get_trade(tid) if tid else None
        out[key] = engine.broker.trade_view(trade) if trade else None
    return jdict(out)


# ------------------------------------------------------------------ LLM


@router.get("/llm/status")
def llm_status(engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    return jdict(engine.llm_status())


@router.get("/llm/calls")
def llm_calls(
    book_id: str | None = None, limit: int = 50, engine: Engine = Depends(get_engine)
) -> list[dict[str, Any]]:
    """Call log: prompt version, latency, tokens, and the failure reason when there was one."""
    return jlist(engine.journal.list_llm_calls(book_id, min(limit, 500)))


# ------------------------------------------------------------------ settings


@router.get("/settings")
def settings(engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    """Read-only view of what the engine is running on. Secrets appear as set/not-set only."""
    cfg = engine.cfg
    books = {}
    for b in cfg.books.values():
        charges = cfg.charges.get(b.charges)
        books[b.id] = {
            "id": b.id,
            "market": b.market,
            "segment": b.segment,
            "style": b.style,
            "currency": b.currency,
            "mode": engine.broker.mode(b.id),
            "starting_capital": b.starting_capital,
            "instruments": b.instruments,
            "timeframes": b.timeframes,
            "risk": b.risk.model_dump(),
            "slippage": b.slippage.model_dump(),
            "copilot": b.copilot.model_dump(),
            "baseline": b.baseline.model_dump(),
            "shadow_raw_strategy": b.shadow_raw_strategy,
            "strategies": [{"id": s.id, "enabled": s.enabled, "params": s.params} for s in b.strategies],
            "charges": {"name": b.charges, **(charges.model_dump() if charges else {})},
            "session": b.session.model_dump() if b.session else None,
        }
    return jdict(
        {
            "books": books,
            "llm": {**cfg.llm.model_dump(exclude={"limits"}), "limits": cfg.llm.limits.model_dump()},
            "llm_runtime": engine.llm_status(),
            "data": {
                "stale_after_seconds": cfg.data.stale_after_seconds,
                "provider": cfg.data.crypto.provider,
                "exchange": cfg.data.crypto.exchange,
            },
            # presence only — the values never leave the process
            "secrets": engine.settings.secrets_status(),
        }
    )
