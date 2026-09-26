from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from papermind.api.deps import get_engine
from papermind.core.serde import jdict, jlist
from papermind.engine import Engine

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    engine = getattr(request.app.state, "engine", None)
    return {"ok": True, "engine": bool(engine and engine.started)}


@router.get("/system/status")
def status(engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    return jdict(engine.status())


class KillBody(BaseModel):
    confirm: bool
    reason: str = "manual kill switch"


@router.post("/system/kill-switch")
async def kill(body: KillBody, engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(400, "confirm must be true")
    return await engine.broker.engage_kill_switch(body.reason)


@router.post("/system/kill-switch/release")
async def release(engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    return await engine.broker.release_kill_switch()


@router.get("/system/audit")
def audit(category: str | None = None, limit: int = 100, engine: Engine = Depends(get_engine)) -> list[Any]:
    return jlist(engine.journal.list_audit(category, min(limit, 1000)))


@router.get("/system/settings")
def settings(engine: Engine = Depends(get_engine)) -> dict[str, Any]:
    """Settings page data. Secrets: presence only — values are never returned."""
    return jdict(
        {
            "charges": {k: v.model_dump() for k, v in engine.cfg.charges.items()},
            "books": {k: v.model_dump() for k, v in engine.cfg.books.items()},
            "data": engine.cfg.data.model_dump(),
            "secrets": engine.settings.secrets_status(),
        }
    )
