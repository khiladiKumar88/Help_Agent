"""FastAPI app factory.  Run:  uvicorn papermind.main:app --reload"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from papermind.api import routes_backtest, routes_books, routes_market, routes_orders, routes_system, ws
from papermind.backtest.jobs import JobManager
from papermind.core.config import Settings, load_config
from papermind.core.logging import setup_logging
from papermind.data.ccxt_provider import CcxtProvider
from papermind.data.history import HistoryStore
from papermind.engine import Engine

log = logging.getLogger(__name__)


def build_engine() -> Engine:
    settings = Settings()
    setup_logging(settings.log_level, settings.secret_values())
    return Engine(load_config(settings), settings)


def create_app(engine_factory: Callable[[], Engine] = build_engine) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = engine_factory()
        hub = ws.WsHub(engine)
        app.state.engine = engine
        app.state.ws_hub = hub
        app.state.history = HistoryStore(engine.cfg.data.history_db_url)
        app.state.jobs = JobManager(engine.db)
        await engine.start()
        hub.start()
        scheduler = AsyncIOScheduler(timezone="UTC")
        if isinstance(engine.provider, CcxtProvider) and not engine.clock.is_replay:
            # daily instrument-master refresh (contract specs can change)
            scheduler.add_job(engine.provider.refresh_instruments, "cron", hour=0, minute=5)
            scheduler.start()
        try:
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            app.state.jobs.shutdown()
            await hub.stop()
            await engine.stop()

    app = FastAPI(title="PaperMind", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in (
        routes_system.router,
        routes_books.router,
        routes_orders.router,
        routes_market.router,
        routes_backtest.router,
        ws.router,
    ):
        app.include_router(r)
    return app


app = create_app()
