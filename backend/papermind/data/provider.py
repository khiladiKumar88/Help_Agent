"""MarketDataProvider interface. Providers are read-only market-data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from papermind.core.types import Candle


class MarketDataProvider(ABC):
    name: str
    exchange: str

    @abstractmethod
    async def start(self) -> None:
        """Load instruments, seed history and start streaming into the MarketHub."""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    def instrument_ids(self) -> list[str]: ...

    @abstractmethod
    async def fetch_candles(
        self, instrument_id: str, timeframe: str, since: datetime | None = None, limit: int = 500
    ) -> list[Candle]:
        """Historical CLOSED candles, oldest first."""

    @abstractmethod
    def status(self) -> dict[str, Any]: ...
