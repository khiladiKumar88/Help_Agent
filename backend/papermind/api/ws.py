"""WebSocket fan-out: ticks, candles, trades, orders, book summaries, risk decisions, status.

Message shape: {"type": <topic>, "data": {...}}. Each client has a bounded queue; a slow
client drops messages instead of blocking the engine. Ticks are throttled per instrument.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from papermind.core.events import Topic
from papermind.core.serde import jsonable
from papermind.core.types import Tick
from papermind.engine import Engine

log = logging.getLogger(__name__)
router = APIRouter()

TICK_MIN_INTERVAL = 0.25
STATUS_INTERVAL = 2.0


class WsHub:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._last_tick: dict[str, datetime] = {}
        self._status_task: asyncio.Task[None] | None = None
        for topic in (
            Topic.CANDLE,
            Topic.TRADE,
            Topic.ORDER,
            Topic.BOOK,
            Topic.RISK_DECISION,
            Topic.SYSTEM,
            Topic.DATA_STALE,
            Topic.DATA_FRESH,
            Topic.SIGNAL,
        ):
            engine.bus.subscribe(topic, self._forwarder(topic))
        engine.bus.subscribe(Topic.TICK, self._on_tick)

    def _forwarder(self, topic: Topic) -> Any:
        def _fwd(payload: Any) -> None:
            self.broadcast({"type": str(topic), "data": jsonable(payload)})

        return _fwd

    def _on_tick(self, tick: Tick) -> None:
        last = self._last_tick.get(tick.instrument_id)
        if last is not None and (tick.ts - last).total_seconds() < TICK_MIN_INTERVAL:
            return
        self._last_tick[tick.instrument_id] = tick.ts
        self.broadcast({"type": "tick", "data": jsonable(tick)})

    def broadcast(self, msg: dict[str, Any]) -> None:
        for q in list(self.clients):
            with contextlib.suppress(asyncio.QueueFull):  # slow client: drop
                q.put_nowait(msg)

    def start(self) -> None:
        self._status_task = asyncio.create_task(self._status_loop(), name="ws-status")

    async def stop(self) -> None:
        if self._status_task:
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._status_task

    async def _status_loop(self) -> None:
        while True:
            try:
                if self.clients:
                    self.broadcast({"type": "status", "data": jsonable(self.engine.status())})
            except Exception:
                log.exception("status broadcast failed")
            await asyncio.sleep(STATUS_INTERVAL)


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    hub: WsHub | None = getattr(ws.app.state, "ws_hub", None)
    await ws.accept()
    if hub is None:
        await ws.close(code=1013)
        return
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
    hub.clients.add(q)
    try:
        await ws.send_json({"type": "status", "data": jsonable(hub.engine.status())})

        async def _sender() -> None:
            while True:
                await ws.send_json(await q.get())

        async def _receiver() -> None:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send_json({"type": "pong", "data": {"ts": hub.engine.clock.now().isoformat()}})

        tasks = [asyncio.create_task(_sender()), asyncio.create_task(_receiver())]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("websocket closed", exc_info=True)
    finally:
        hub.clients.discard(q)
