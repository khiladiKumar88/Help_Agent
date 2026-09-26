"""Non-negotiable rule #1: paper only.

Static check: no module in papermind/ may import or call any order-placement, cancellation,
account or fund-movement API of a broker SDK or exchange library, nor pass credentials to one.
Runtime check: the exchange wrapper refuses those calls and refuses credentials.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import ccxt
import pytest

from papermind.data.ccxt_provider import default_exchange_factory
from papermind.data.public_only import ForbiddenExchangeOperation, PublicOnlyExchange

PKG = Path(__file__).resolve().parents[1] / "papermind"

FORBIDDEN_NAME = re.compile(
    r"""^(
        place_?order | placeorder | modify_?order | cancel_?order | cancelorder |
        create(_\w+)?_?order(s)?(_?ws)? | create\w*Order\w* | edit_?order | editOrder |
        cancel\w*Orders? | withdraw\w* | transfer\w* | fetch_?balance | fetchBalance |
        set_?leverage | setLeverage | set_?margin_?mode | setMarginMode | fetch_?my_?trades | fetchMyTrades |
        placeOrder | modifyOrder | convertPosition | convert_?position | gttCreateRule | gtt_?create_?rule
    )$""",
    re.VERBOSE | re.IGNORECASE,
)
CREDENTIAL_KEYS = {"apiKey", "secret", "password", "privateKey", "uid", "api_key", "api_secret"}


def scan_source(source: str, filename: str = "<src>") -> list[str]:
    tree = ast.parse(source, filename)
    problems: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if FORBIDDEN_NAME.match(alias.name):
                    problems.append(f"{filename}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                name = node.args[1].value
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and k.value in CREDENTIAL_KEYS:
                    problems.append(f"{filename}:{node.lineno} passes credential key '{k.value}'")
        if name and FORBIDDEN_NAME.match(name):
            problems.append(f"{filename}:{getattr(node, 'lineno', '?')} uses '{name}'")
    return problems


def test_scanner_catches_violations() -> None:
    bad = (
        "from SmartApi import SmartConnect\n"
        "s = SmartConnect(api_key='x')\n"
        "s.placeOrder({'variety': 'NORMAL'})\n"
        "ex.create_order('BTC/USDT', 'market', 'buy', 1)\n"
        "ex.createMarketBuyOrder('BTC/USDT', 1)\n"
        "getattr(ex, 'cancel_order')('1')\n"
        "ccxt.binance({'apiKey': 'k', 'secret': 's'})\n"
        "from x import place_order\n"
    )
    problems = scan_source(bad)
    joined = "\n".join(problems)
    for needle in ("placeOrder", "create_order", "createMarketBuyOrder", "cancel_order", "apiKey", "place_order"):
        assert needle in joined, f"scanner missed {needle}"


def test_codebase_has_no_order_placement() -> None:
    problems: list[str] = []
    files = sorted(PKG.rglob("*.py"))
    assert files, "no source files found"
    for f in files:
        problems += scan_source(f.read_text(), str(f.relative_to(PKG.parent)))
    assert not problems, "order-placement / credential usage found:\n" + "\n".join(problems)


def test_wrapper_blocks_trading_calls_on_real_ccxt_instance() -> None:
    ex = PublicOnlyExchange(ccxt.binanceusdm())
    for method in (
        "create_order",
        "createOrder",
        "cancel_order",
        "fetch_balance",
        "withdraw",
        "transfer",
        "set_leverage",
        "edit_order",
        "create_market_buy_order",
    ):
        with pytest.raises(ForbiddenExchangeOperation):
            getattr(ex, method)
    assert callable(ex.fetch_ohlcv)
    assert callable(ex.load_markets)


def test_wrapper_refuses_credentials() -> None:
    with pytest.raises(ForbiddenExchangeOperation):
        PublicOnlyExchange(ccxt.binanceusdm({"apiKey": "abc", "secret": "def"}))


def test_wrapper_is_immutable() -> None:
    ex = PublicOnlyExchange(ccxt.binanceusdm())
    with pytest.raises(ForbiddenExchangeOperation):
        ex.apiKey = "x"


async def test_default_factory_builds_credential_free_exchange() -> None:
    raw = default_exchange_factory("binanceusdm")
    try:
        assert not raw.apiKey and not raw.secret
        PublicOnlyExchange(raw)  # does not raise
    finally:
        await raw.close()
