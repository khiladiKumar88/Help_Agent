"""API + WebSocket end-to-end with a fixture-backed provider (no network, no background feed).

This is the Phase-1 acceptance flow: place a manual paper trade on the BTC perp and see
live P&L after fees, then close it; plus kill switch and error states.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from papermind.core.config import Settings
from papermind.core.types import Tick
from papermind.data.provider import MarketDataProvider
from papermind.engine import Engine
from papermind.main import create_app
from tests.conftest import BOOK_ID, BTC, ccxt_instruments, make_cfg


class FixtureProvider(MarketDataProvider):
    name = "fixture"
    exchange = "binanceusdm"

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.connected = False

    async def start(self) -> None:
        self.engine.registry.upsert(ccxt_instruments(), self.engine.clock.now())
        self.connected = True

    async def stop(self) -> None:
        self.connected = False

    def instrument_ids(self) -> list[str]:
        return [i.id for i in ccxt_instruments()]

    async def fetch_candles(self, instrument_id: str, timeframe: str, since: Any = None, limit: int = 500) -> list[Any]:
        return []

    def status(self) -> dict[str, Any]:
        return {"provider": self.name, "exchange": self.exchange, "connected": self.connected, "feed_mode": "fixture"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    cfg = make_cfg(db_url=f"sqlite:///{tmp_path / 'api.db'}")
    settings = Settings(gemini_api_key="sk-should-never-leak", _env_file=None)  # type: ignore[call-arg]
    app = create_app(lambda: Engine(cfg, settings, provider_factory=FixtureProvider))
    with TestClient(app) as c:
        yield c


def engine_of(c: TestClient) -> Engine:
    e: Engine = c.app.state.engine  # type: ignore[attr-defined]
    return e


def push_tick(c: TestClient, bid: str, ask: str, inst: str = BTC) -> None:
    e = engine_of(c)
    t = Tick(instrument_id=inst, ts=datetime.now(UTC), ltp=Decimal(ask), bid=Decimal(bid), ask=Decimal(ask))
    assert c.portal is not None
    c.portal.call(e.hub.on_tick, t)


ORDER = {
    "book_id": BOOK_ID,
    "instrument_id": BTC,
    "direction": "long",
    "qty": "0.005",
    "stop_loss": "64000",
    "target": "66000",
    "leverage": "3",
}


def test_health_and_status(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"ok": True, "engine": True}
    st = client.get("/api/system/status").json()
    assert st["books"][BOOK_ID]["mode"] == "manual"
    assert st["books"][BOOK_ID]["status"] == "stale"  # no ticks yet
    assert st["secrets"]["gemini_api_key"] is True
    assert "sk-should-never-leak" not in client.get("/api/system/status").text
    assert "sk-should-never-leak" not in client.get("/api/system/settings").text
    assert st["kill_switch"] == {"engaged": False}


def test_books_instruments_and_config(client: TestClient) -> None:
    books = client.get("/api/books").json()
    assert books[0]["id"] == BOOK_ID and books[0]["currency"] == "USDT"
    insts = client.get("/api/market/instruments", params={"book_id": BOOK_ID}).json()
    assert {i["id"] for i in insts} == {BTC, "crypto:binanceusdm:ETH/USDT:USDT"}
    assert len(client.get("/api/market/instruments").json()) == 2
    cfg = client.get(f"/api/books/{BOOK_ID}/config").json()
    assert cfg["charges"]["taker_pct"] == "0.05" and cfg["charges"]["verified"] is False
    assert client.get("/api/books/nope").status_code == 404
    assert client.get("/api/market/quote", params={"instrument_id": "x"}).status_code == 404
    assert client.get("/api/market/candles", params={"instrument_id": "x"}).status_code == 404


def test_manual_trade_lifecycle_with_live_pnl_after_fees(client: TestClient) -> None:
    push_tick(client, "64999.9", "65000")
    q = client.get("/api/market/quote", params={"instrument_id": BTC}).json()
    assert q["stale"] is False and q["tick"]["ask"] == "65000"

    prev = client.post("/api/orders/preview", json=ORDER).json()
    assert prev["decision"]["approved"] is True and prev["max_qty_by_risk"] == "0.009"

    r = client.post("/api/orders", json=ORDER).json()
    assert r["approved"] is True and r["trade"]["status"] == "pending" and r["trade"]["actor"] == "human"
    tid = r["trade"]["id"]

    push_tick(client, "64999.9", "65000")  # fills on this (next) tick
    push_tick(client, "65500", "65500.1")
    book = client.get(f"/api/books/{BOOK_ID}").json()
    pos = book["positions"][0]
    assert pos["status"] == "open" and pos["avg_entry"] == "65013"
    assert Decimal(pos["unrealized_net"]) == Decimal("2.43500000") - Decimal("0.1625325") - Decimal("0.16375")
    assert pos["unrealized_r"] == "0.42" and pos["stale"] is False
    assert Decimal(book["equity"]) > Decimal(book["balance"])
    assert Decimal(book["used_margin"]) == Decimal("65013") * Decimal("0.005") / 3

    m = client.patch(f"/api/trades/{tid}", json={"stop_loss": "64600"})
    assert m.status_code == 200 and m.json()["current_sl"] == "64600"
    bad = client.patch(f"/api/trades/{tid}", json={"stop_loss": "70000"})
    assert bad.status_code == 422 and bad.json()["detail"]["rule_id"] == "R005_STOP_LOSS_VALID"

    assert client.post(f"/api/trades/{tid}/close").status_code == 200
    push_tick(client, "65600", "65600.1")
    detail = client.get(f"/api/trades/{tid}").json()
    assert detail["status"] == "closed" and detail["exit_reason"] == "manual"
    assert len(detail["fills"]) == 2 and detail["fills"][0]["liquidity"] == "taker"
    assert Decimal(detail["net_pnl"]) == Decimal(detail["gross_pnl"]) - Decimal(detail["charges"])
    assert client.post(f"/api/trades/{tid}/close").status_code == 422
    assert client.post(f"/api/trades/{tid}/cancel").status_code == 422
    assert client.get("/api/trades/nope").status_code == 404

    trades = client.get(f"/api/books/{BOOK_ID}/trades", params={"status": "closed"}).json()
    assert [t["id"] for t in trades] == [tid]
    assert client.get(f"/api/books/{BOOK_ID}/equity").json()  # snapshot written on close
    pnl = client.get(f"/api/books/{BOOK_ID}/actor-pnl").json()
    assert pnl["series"]["human"] and pnl["series"]["agent"] == []
    assert client.get(f"/api/books/{BOOK_ID}/risk-decisions").json()[0]["approved"] is True
    assert any(a["category"] == "TRADE" for a in client.get("/api/system/audit").json())


def test_rejected_order_is_reported(client: TestClient) -> None:
    push_tick(client, "64999.9", "65000")
    r = client.post("/api/orders", json={**ORDER, "qty": "0.05"}).json()
    assert r["approved"] is False and r["trade"] is None
    assert r["decision"]["failed_rule_id"] == "R006_MAX_RISK_PER_TRADE"
    rej = client.get(f"/api/books/{BOOK_ID}/risk-decisions", params={"rejected_only": True}).json()
    assert rej[0]["failed_rule_id"] == "R006_MAX_RISK_PER_TRADE"
    assert client.post("/api/orders", json={**ORDER, "qty": "-1"}).status_code == 422


def test_pending_cancel(client: TestClient) -> None:
    push_tick(client, "64999.9", "65000")
    r = client.post(
        "/api/orders", json={**ORDER, "order_type": "limit", "limit_price": "64000", "stop_loss": "63000"}
    ).json()
    c = client.post(f"/api/trades/{r['trade']['id']}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelled"


def test_modes(client: TestClient) -> None:
    assert client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "copilot"}).status_code == 409
    assert client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "manual"}).json()["mode"] == "manual"


def test_kill_switch(client: TestClient) -> None:
    push_tick(client, "64999.9", "65000")
    client.post("/api/orders", json=ORDER)
    push_tick(client, "64999.9", "65000")
    assert client.post("/api/system/kill-switch", json={"confirm": False}).status_code == 400
    res = client.post("/api/system/kill-switch", json={"confirm": True, "reason": "test"}).json()
    assert res["engaged"] is True and res["closed"] == 1
    book = client.get(f"/api/books/{BOOK_ID}").json()
    assert book["positions"] == [] and book["halted"] is True and book["kill_switch"] is True
    assert client.get("/api/system/status").json()["books"][BOOK_ID]["status"] == "halted"
    r = client.post("/api/orders", json=ORDER).json()
    assert r["decision"]["failed_rule_id"] == "R001_KILL_SWITCH"
    assert client.post("/api/system/kill-switch/release").json() == {"engaged": False}
    assert client.get(f"/api/books/{BOOK_ID}").json()["halted"] is False


def test_websocket_stream(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and "books" in first["data"]
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        push_tick(client, "64999.9", "65000")
        types = set()
        for _ in range(5):
            msg = ws.receive_json()
            types.add(msg["type"])
            if msg["type"] == "tick":
                assert msg["data"]["instrument_id"] == BTC and msg["data"]["ask"] == "65000"
                break
        assert "tick" in types
        client.post("/api/orders", json=ORDER)
        seen = {ws.receive_json()["type"] for _ in range(3)}
        assert {"risk_decision", "trade"} & seen


def test_candles_endpoint(client: TestClient) -> None:
    push_tick(client, "64999.9", "65000")
    body = client.get("/api/market/candles", params={"instrument_id": BTC}).json()
    assert body["closed"] == [] and body["forming"]["close"] == "65000"


def test_engine_not_running_returns_503(tmp_path: Path) -> None:
    app = create_app(
        lambda: Engine(make_cfg(db_url=f"sqlite:///{tmp_path / 'x.db'}"), provider_factory=FixtureProvider)
    )
    c = TestClient(app)  # no context manager -> lifespan not run
    assert c.get("/api/books").status_code == 503
    assert c.get("/api/health").json()["engine"] is False
