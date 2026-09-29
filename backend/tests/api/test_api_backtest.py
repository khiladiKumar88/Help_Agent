"""Backtest/history/strategy API with the offline simulated history source."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from papermind.core.config import Settings
from papermind.engine import Engine
from papermind.main import create_app
from tests.api.test_api import FixtureProvider
from tests.conftest import BOOK_ID, book_dict, make_cfg


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    cfg = make_cfg(
        {**book_dict(), "strategies": [{"id": "supertrend_flip"}]},
        db_url=f"sqlite:///{tmp_path / 'a.db'}",
        data={"history_db_url": f"sqlite:///{tmp_path / 'h.db'}"},
    )
    app = create_app(lambda: Engine(cfg, Settings(_env_file=None), provider_factory=FixtureProvider))  # type: ignore[call-arg]
    with TestClient(app) as c:
        yield c


def wait(c: TestClient, run_id: str) -> dict:  # type: ignore[type-arg]
    jobs = c.app.state.jobs  # type: ignore[attr-defined]
    row = jobs.wait(run_id, timeout=120)
    assert row is not None
    return c.get(f"/api/backtests/{run_id}").json()  # type: ignore[no-any-return]


def test_strategies_listed(client: TestClient) -> None:
    rows = client.get("/api/strategies").json()
    ids = {r["id"] for r in rows}
    assert ids == {"orb", "vwap_reclaim", "ema_pullback", "supertrend_flip", "rsi_mean_reversion", "funding_fade"}
    assert all(r["grid_size"] <= 16 and r["description"] for r in rows)


def test_download_coverage_backtest_and_replay(client: TestClient) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    start = (now - timedelta(days=16)).isoformat()
    r = client.post(
        "/api/history/download",
        json={"exchange": "simulated", "symbol": "BTC/USDT:USDT", "timeframe": "15m", "start": start},
    )
    job = wait(client, r.json()["run_id"])
    assert job["status"] == "done" and "Downloaded" in job["result"]["summary"]
    cov = client.get(
        "/api/history/coverage", params={"exchange": "simulated", "symbol": "BTC/USDT:USDT", "timeframe": "15m"}
    ).json()
    assert cov["bars"] > 1400 and cov["missing_bars"] == 0 and cov["gaps"] == []
    assert client.get("/api/history/datasets").json()[0]["timeframe"] == "15m"

    body = {
        "kind": "backtest",
        "book_id": BOOK_ID,
        "exchange": "simulated",
        "symbol": "BTC/USDT:USDT",
        "base_tf": "15m",
        "strategy_id": "supertrend_flip",
        "start": (now - timedelta(days=10)).isoformat(),
        "end": (now - timedelta(days=1)).isoformat(),
    }
    bt = wait(client, client.post("/api/backtests", json=body).json()["run_id"])
    assert bt["status"] == "done", bt["error"]
    res = bt["result"]
    assert res["agent"]["trades"] >= 1 and "Beat the baseline" in res["summary"] and res["buy_hold"] is not None
    listed = client.get("/api/backtests").json()
    assert listed[0]["id"] == bt["id"] and listed[0]["verdict"] is not None

    rp = {
        "kind": "replay",
        "book_id": BOOK_ID,
        "exchange": "simulated",
        "symbol": "BTC/USDT:USDT",
        "base_tf": "15m",
        "day": (now - timedelta(days=3)).isoformat(),
    }
    rep = wait(client, client.post("/api/backtests", json=rp).json()["run_id"])
    assert rep["status"] == "done" and rep["result"]["kind"] == "replay" and len(rep["result"]["candles"]) == 96

    missing = {**body, "start": "2021-01-01T00:00:00Z", "end": "2021-01-05T00:00:00Z"}
    err = wait(client, client.post("/api/backtests", json=missing).json()["run_id"])
    assert err["status"] == "error" and "download" in err["error"]


def test_validation_errors(client: TestClient) -> None:
    base = {
        "kind": "backtest",
        "book_id": BOOK_ID,
        "exchange": "simulated",
        "symbol": "BTC/USDT:USDT",
        "strategy_id": "nope",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-05T00:00:00Z",
    }
    assert client.post("/api/backtests", json=base).status_code == 422
    assert client.post("/api/backtests", json={**base, "strategy_id": "orb", "end": None}).status_code == 422
    assert client.post("/api/backtests", json={**base, "kind": "replay"}).status_code == 422
    assert client.post("/api/backtests", json={**base, "book_id": "x"}).status_code == 404
    wf = {**base, "kind": "walkforward", "strategy_id": "orb", "train_days": 2}
    assert client.post("/api/backtests", json=wf).status_code == 422
    assert client.get("/api/backtests/nope").status_code == 404
    assert client.post("/api/backtests/nope/cancel").json() == {"cancelled": False}
    assert (
        client.post(
            "/api/history/download",
            json={"exchange": "simulated", "symbol": "x", "timeframe": "7x", "start": "2026-01-01T00:00:00Z"},
        ).status_code
        == 422
    )


def test_signals_endpoint(client: TestClient) -> None:
    assert client.get(f"/api/books/{BOOK_ID}/signals").json() == []
    assert client.get("/api/books/nope/signals").status_code == 404
