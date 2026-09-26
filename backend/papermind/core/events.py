"""In-process async event bus with typed topics.

Handlers must never crash the publisher: exceptions are logged and swallowed (fail safe).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class Topic(StrEnum):
    TICK = "tick"
    CANDLE = "candle"
    FUNDING = "funding"
    DATA_STALE = "data_stale"
    DATA_FRESH = "data_fresh"
    ORDER = "order"
    TRADE = "trade"
    RISK_DECISION = "risk_decision"
    BOOK = "book"
    SYSTEM = "system"


Handler = Callable[[Any], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[Topic, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: Topic, handler: Handler) -> Callable[[], None]:
        self._subs[topic].append(handler)

        def _unsubscribe() -> None:
            if handler in self._subs[topic]:
                self._subs[topic].remove(handler)

        return _unsubscribe

    async def publish(self, topic: Topic, payload: Any) -> None:
        for handler in list(self._subs[topic]):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event handler failed", extra={"topic": str(topic)})
