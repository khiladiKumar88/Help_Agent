from __future__ import annotations

from fastapi import HTTPException, Request

from papermind.engine import Engine


def get_engine(request: Request) -> Engine:
    engine: Engine | None = getattr(request.app.state, "engine", None)
    if engine is None or not engine.started:
        raise HTTPException(503, "engine not running")
    return engine


def require_book(engine: Engine, book_id: str) -> None:
    if book_id not in engine.cfg.books:
        raise HTTPException(404, f"unknown book '{book_id}'")
