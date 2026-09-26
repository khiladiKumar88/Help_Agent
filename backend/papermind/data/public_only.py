"""Runtime guard: wraps a ccxt exchange so ONLY public market-data calls are possible.

PaperMind is paper-only. Order placement, cancellation, balances, transfers and
withdrawals are blocked here (and by tests/test_no_order_placement.py statically).
Credentials are refused outright, so even a bypass could not authenticate.
"""

from __future__ import annotations

from typing import Any

ALLOWED_METHODS = frozenset(
    {
        "load_markets",
        "fetch_markets",
        "fetch_time",
        "fetch_status",
        "fetch_ticker",
        "fetch_tickers",
        "fetch_bids_asks",
        "fetch_order_book",
        "fetch_ohlcv",
        "fetch_trades",
        "fetch_funding_rate",
        "fetch_funding_rates",
        "fetch_funding_rate_history",
        "fetch_open_interest",
        "fetch_mark_ohlcv",
        "watch_ticker",
        "watch_tickers",
        "watch_bids_asks",
        "watch_order_book",
        "watch_ohlcv",
        "watch_trades",
        "milliseconds",
        "close",
    }
)
ALLOWED_ATTRS = frozenset({"id", "markets", "symbols", "has", "timeframes", "precisionMode", "rateLimit", "name"})
CREDENTIAL_KEYS = frozenset(
    {"apiKey", "secret", "password", "uid", "privateKey", "walletAddress", "token", "login", "twofa"}
)


class ForbiddenExchangeOperation(RuntimeError):
    """Raised when code tries to use a non-public (trading/account) exchange method."""


class PublicOnlyExchange:
    def __init__(self, exchange: Any) -> None:
        for key in CREDENTIAL_KEYS:
            if getattr(exchange, key, None):
                raise ForbiddenExchangeOperation(f"exchange was configured with credential '{key}' — refused")
        object.__setattr__(self, "_ex", exchange)

    def __getattr__(self, name: str) -> Any:
        if name in ALLOWED_METHODS or name in ALLOWED_ATTRS:
            return getattr(object.__getattribute__(self, "_ex"), name)
        raise ForbiddenExchangeOperation(f"'{name}' is not a whitelisted public market-data call")

    def __setattr__(self, name: str, value: Any) -> None:
        raise ForbiddenExchangeOperation(f"setting '{name}' on the exchange wrapper is not allowed")
