"""History data, strategies, backtests / walk-forward / replay jobs, and scanner signals."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from papermind.api.deps import get_engine, require_book
from papermind.backtest.jobs import JobManager
from papermind.backtest.runner import (
    BacktestSpec,
    ReplaySpec,
    WalkForwardSpec,
    run_backtest,
    run_replay,
    run_walkforward,
)
from papermind.core.clock import RealClock
from papermind.core.serde import jdict, jlist
from papermind.data.candles import tf_seconds
from papermind.data.history import HistoryDownloader, HistoryStore, default_history_exchange_factory
from papermind.engine import Engine
from papermind.strategies.base import grid_size
from papermind.strategies.builtin import STRATEGIES

router = APIRouter(prefix="/api", tags=["backtest"])


def get_store(request: Request) -> HistoryStore:
    store: HistoryStore | None = getattr(request.app.state, "history", None)
    if store is None:
        raise HTTPException(503, "history store not available")
    return store


def get_jobs(request: Request) -> JobManager:
    jobs: JobManager | None = getattr(request.app.state, "jobs", None)
    if jobs is None:
        raise HTTPException(503, "job runner not available")
    return jobs


# ------------------------------------------------------------------ history data


@router.get("/history/datasets")
def datasets(store: HistoryStore = Depends(get_store)) -> list[dict[str, Any]]:
    return jlist(store.datasets())


@router.get("/history/coverage")
def coverage(exchange: str, symbol: str, timeframe: str, store: HistoryStore = Depends(get_store)) -> dict[str, Any]:
    c = store.coverage(exchange, symbol, timeframe)
    return jdict(
        {
            "exchange": c.exchange,
            "symbol": c.symbol,
            "timeframe": c.timeframe,
            "first": c.first,
            "last": c.last,
            "bars": c.bars,
            "expected_bars": c.expected_bars,
            "missing_bars": c.missing_bars,
            "complete_pct": c.complete_pct,
            "gaps": [{"start": g.start, "end": g.end, "missing_bars": g.missing_bars} for g in c.gaps],
        }
    )


class DownloadBody(BaseModel):
    exchange: str
    symbol: str
    timeframe: str = "5m"
    start: datetime
    end: datetime | None = None
    repair_gaps: bool = False


@router.post("/history/download")
def download(
    body: DownloadBody,
    store: HistoryStore = Depends(get_store),
    jobs: JobManager = Depends(get_jobs),
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    try:
        tf_seconds(body.timeframe)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    factory = getattr(engine, "history_exchange_factory", None) or default_history_exchange_factory(RealClock())

    async def job(progress: Any, _cancelled: Any) -> dict[str, Any]:
        dl = HistoryDownloader(store, factory, RealClock())
        rep = await dl.sync(
            body.exchange, body.symbol, body.timeframe, body.start, body.end, body.repair_gaps, progress
        )
        cov = rep.coverage
        return {
            "summary": f"Downloaded {rep.bars_written} bars ({rep.requests} requests) for {body.symbol} "
            f"{body.timeframe}; stored range {cov.first if cov else None} → {cov.last if cov else None}, "
            f"{cov.missing_bars if cov else 0} missing bars.",
            "bars_written": rep.bars_written,
            "requests": rep.requests,
            "funding_written": rep.funding_written,
        }

    return {"run_id": jobs.submit("download", body.model_dump(mode="json"), job)}


# ------------------------------------------------------------------ strategies


@router.get("/strategies")
def strategies() -> list[dict[str, Any]]:
    return jlist(
        [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "markets": sorted(str(m) for m in s.markets),
                "default_params": s.default_params,
                "param_grid": s.param_grid,
                "grid_size": grid_size(s.param_grid),
            }
            for s in STRATEGIES.values()
        ]
    )


# ------------------------------------------------------------------ backtests / walk-forward / replay


class JobBody(BaseModel):
    kind: Literal["backtest", "walkforward", "replay"]
    book_id: str
    exchange: str
    symbol: str
    base_tf: str = "5m"
    strategy_id: str | None = None
    params: dict[str, float | int] = Field(default_factory=dict)
    start: datetime | None = None
    end: datetime | None = None
    day: datetime | None = None
    train_days: int = 60
    test_days: int = 30
    min_trades: int = 30
    speed: float = 0.0


@router.post("/backtests")
def start_job(
    body: JobBody,
    engine: Engine = Depends(get_engine),
    store: HistoryStore = Depends(get_store),
    jobs: JobManager = Depends(get_jobs),
) -> dict[str, Any]:
    require_book(engine, body.book_id)
    cfg = engine.cfg
    try:
        if body.kind == "replay":
            if body.day is None:
                raise ValueError("replay needs a day")
            rspec = ReplaySpec(
                book_id=body.book_id,
                exchange=body.exchange,
                symbol=body.symbol,
                day=body.day,
                base_tf=body.base_tf,
                speed=body.speed,
            )

            async def job(progress: Any, cancelled: Any) -> dict[str, Any]:
                return await run_replay(cfg, store, rspec, progress, cancelled)

            spec_dump = rspec.model_dump(mode="json")
        elif body.kind == "walkforward":
            wspec = WalkForwardSpec(
                book_id=body.book_id,
                exchange=body.exchange,
                symbol=body.symbol,
                base_tf=body.base_tf,
                strategy_id=body.strategy_id or "",
                start=body.start,
                end=body.end,
                train_days=body.train_days,
                test_days=body.test_days,
                min_trades=body.min_trades,
            )

            async def job(progress: Any, cancelled: Any) -> dict[str, Any]:
                return await run_walkforward(cfg, store, wspec, progress, cancelled)

            spec_dump = wspec.model_dump(mode="json")
        else:
            bspec = BacktestSpec(
                book_id=body.book_id,
                exchange=body.exchange,
                symbol=body.symbol,
                base_tf=body.base_tf,
                strategy_id=body.strategy_id or "",
                params=body.params,
                start=body.start,
                end=body.end,
                min_trades=body.min_trades,
            )

            async def job(progress: Any, cancelled: Any) -> dict[str, Any]:
                return await run_backtest(cfg, store, bspec, progress, cancelled)

            spec_dump = bspec.model_dump(mode="json")
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.kind != "replay" and body.strategy_id not in STRATEGIES:
        raise HTTPException(422, f"unknown strategy '{body.strategy_id}'")
    return {"run_id": jobs.submit(body.kind, spec_dump, job)}


@router.get("/backtests")
def list_jobs(limit: int = 30, jobs: JobManager = Depends(get_jobs)) -> list[dict[str, Any]]:
    return jobs.list(min(limit, 200))


@router.get("/backtests/{run_id}")
def get_job(run_id: str, jobs: JobManager = Depends(get_jobs)) -> dict[str, Any]:
    row = jobs.get(run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    return row


@router.post("/backtests/{run_id}/cancel")
def cancel_job(run_id: str, jobs: JobManager = Depends(get_jobs)) -> dict[str, Any]:
    return {"cancelled": jobs.cancel(run_id)}


# ------------------------------------------------------------------ live scanner signals


@router.get("/books/{book_id}/signals")
def signals(book_id: str, limit: int = 30, engine: Engine = Depends(get_engine)) -> list[dict[str, Any]]:
    require_book(engine, book_id)
    return jlist(engine.journal.list_signals(book_id, min(limit, 500)))
