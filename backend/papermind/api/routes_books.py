from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from papermind.api.deps import get_engine, require_book
from papermind.backtest.metrics import trade_metrics
from papermind.core.config import Mode
from papermind.core.money import ZERO
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
    confirm: bool = False  # Auto needs an explicit confirmation (the UI shows a dialog)


@router.put("/{book_id}/mode")
def set_mode(book_id: str, body: ModeBody, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    require_book(engine, book_id)
    if body.mode is not Mode.MANUAL and engine.analyst is None:
        # No analyst => no verdicts => the agent would be trading blind. Refuse, and say why.
        raise HTTPException(
            409,
            {
                "message": f"mode '{body.mode}' needs the LLM analyst, which is not available",
                "reason": engine.llm_error or "llm.provider is 'off'",
            },
        )
    if body.mode is Mode.AUTO and not body.confirm:
        raise HTTPException(
            428,
            {
                "message": "Auto mode places paper trades without asking. Confirm to switch.",
                "needs_confirm": True,
            },
        )
    engine.broker.set_mode(book_id, str(body.mode))
    engine.journal.audit("MODE", f"mode set to {body.mode}", book_id=book_id)
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


# Which (account, actor) pair each comparison series is built from. Shadow trades are tagged
# actor=agent, so grouping by actor alone would fold them into the agent's own curve.
SERIES_SOURCES: tuple[tuple[str, Account, str | None], ...] = (
    ("human", Account.MAIN, "human"),  # trades you placed from the order ticket
    ("agent", Account.MAIN, "agent"),  # what the LLM-filtered agent actually traded
    ("baseline", Account.SHADOW_BASELINE, None),  # random entries, same risk and frequency
    ("raw_strategy", Account.SHADOW_RAW, None),  # EVERY signal, unfiltered
    ("rejected", Account.SHADOW_REJECTED, None),  # cards you vetoed, shadow-tracked
)


def _series_for(engine: Engine, book_id: str, account: Account, actor: str | None) -> list[Any]:
    return [
        t for t in engine.journal.list_trades(book_id, account, status="closed", actor=actor, limit=5000) if t.closed_at
    ]


@router.get("/{book_id}/actor-pnl")
def actor_pnl(book_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    """Cumulative net P&L of closed trades, per comparison series (You vs Agent vs Baseline vs ...)."""
    require_book(engine, book_id)
    series: dict[str, list[dict[str, Any]]] = {}
    totals: dict[str, Any] = {}
    for name, account, actor in SERIES_SOURCES:
        closed = _series_for(engine, book_id, account, actor)
        closed.sort(key=lambda t: t.closed_at)
        running = ZERO
        points: list[dict[str, Any]] = []
        for t in closed:
            running += t.net_pnl
            points.append({"ts": t.closed_at, "cum_net_pnl": running, "r": t.r_multiple})
        series[name] = points
        totals[name] = running
    return jdict({"series": series, "totals": totals})


@router.get("/{book_id}/edge")
def edge(book_id: str, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    """Does the LLM's TAKE/SKIP filter actually help?

    Compares the same risk rules applied to: every signal (`raw_strategy`), only the signals the
    analyst approved (`agent`), the human's own trades, the random baseline, and the cards the
    human rejected. `skipped` counts the signals the analyst turned down — the whole point of
    the comparison is whether skipping them was worth it.
    """
    require_book(engine, book_id)
    book = engine.cfg.book(book_id)
    rows: dict[str, Any] = {}
    for name, account, actor in SERIES_SOURCES:
        closed = _series_for(engine, book_id, account, actor)
        rows[name] = trade_metrics(closed, book.starting_capital).as_dict()
    signals = engine.journal.list_signals(book_id, limit=100_000)
    decided = [s for s in signals if s["decision"] in ("take", "skip")]
    return jdict(
        {
            "book_id": book_id,
            "currency": book.currency,
            "series": rows,
            "signals": {
                "total": len(signals),
                "taken": sum(1 for s in decided if s["decision"] == "take"),
                "skipped": sum(1 for s in decided if s["decision"] == "skip"),
                "pending": len(signals) - len(decided),
                "blocked_by_risk": sum(1 for s in signals if s["risk_rule_blocked"]),
            },
            "cards": {
                status: sum(1 for s in signals if s["card_status"] == status)
                for status in ("pending", "approved", "rejected", "expired")
            },
            "note": (
                "raw_strategy trades every signal; agent trades only those the analyst approved. "
                "If agent is not better than raw_strategy, the analyst is not adding anything."
            ),
        }
    )
