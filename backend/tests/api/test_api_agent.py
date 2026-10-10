"""Phase-3 API: mode switching, co-pilot cards, journal, LLM status/calls, settings.

The engine runs with the deterministic FakeProvider analyst, so none of this touches Gemini.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from papermind.core.config import Settings
from papermind.core.ids import new_id
from papermind.core.types import Direction, Signal, Tick
from papermind.engine import Engine, EngineOptions
from papermind.llm.fake import FakeProvider
from papermind.main import create_app
from tests.api.test_api import FixtureProvider
from tests.conftest import BOOK_ID, BTC, make_cfg

D = Decimal

TAKE = json.dumps(
    {
        "decision": "TAKE",
        "confidence": 88,
        "reasoning": "Trend intact, pullback held.",
        "risks_noted": ["ADX may roll over"],
        "lessons_applied": [],
        "suggested_adjustments": {},
    }
)
SKIP = json.dumps({"decision": "SKIP", "confidence": 90, "reasoning": "Chop.", "risks_noted": []})


def llm_block(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": "fake",
        "model": "test-model",
        "limits": {"requests_per_minute": 600, "requests_per_day": 50, "tokens_per_minute": None},
    }
    base.update(over)
    return base


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    cfg = make_cfg(db_url=f"sqlite:///{tmp_path / 'agent.db'}", llm=llm_block())
    settings = Settings(gemini_api_key="sk-should-never-leak", _env_file=None)  # type: ignore[call-arg]
    provider = FakeProvider(replies=[TAKE])

    def build() -> Engine:
        return Engine(
            cfg,
            settings,
            provider_factory=FixtureProvider,
            options=EngineOptions(llm_provider=provider),
        )

    app = create_app(build)
    with TestClient(app) as c:
        c.fake_llm = provider  # type: ignore[attr-defined]
        yield c


def engine_of(c: TestClient) -> Engine:
    e: Engine = c.app.state.engine  # type: ignore[attr-defined]
    return e


def push_tick(c: TestClient, price: str = "65000") -> None:
    e = engine_of(c)
    t = Tick(instrument_id=BTC, ts=datetime.now(UTC), ltp=D(price), bid=D(price), ask=D(price))
    assert c.portal is not None
    c.portal.call(e.hub.on_tick, t)


def emit_signal(c: TestClient, **over: Any) -> str:
    """Save a signal and run it through the real executor, as the scanner would."""
    e = engine_of(c)
    fields: dict[str, Any] = {
        "id": new_id("sig"),
        "ts": e.clock.now(),
        "book_id": BOOK_ID,
        "instrument_id": BTC,
        "strategy_id": "ema_pullback",
        "timeframe": "15m",
        "direction": Direction.LONG,
        "entry_ref": D("65000"),
        "stop_loss": D("64000"),
        "target": D("67000"),
        "setup": "pullback to EMA21",
        "regime": {"label": "trending_up", "adx": 30.0, "time_bucket": "europe", "expiry_day": False},
        "context": {"close": 65010.0, "rsi14": 57.0, "adx14": 30.0},
    }
    fields.update(over)
    sig = Signal.model_validate(fields)
    e.journal.save_signal(sig)
    assert c.portal is not None
    c.portal.call(e.executor.on_signal, sig)
    return sig.id


class TestModes:
    def test_copilot_and_auto_are_available_once_the_analyst_exists(self, client: TestClient) -> None:
        assert engine_of(client).analyst is not None
        r = client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "copilot"})
        assert r.status_code == 200 and r.json()["mode"] == "copilot"

    def test_auto_requires_an_explicit_confirmation(self, client: TestClient) -> None:
        r = client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "auto"})
        assert r.status_code == 428
        assert r.json()["detail"]["needs_confirm"] is True
        assert engine_of(client).broker.mode(BOOK_ID) != "auto"

    def test_auto_switches_once_confirmed(self, client: TestClient) -> None:
        r = client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "auto", "confirm": True})
        assert r.status_code == 200
        assert engine_of(client).broker.mode(BOOK_ID) == "auto"

    def test_manual_never_needs_confirmation(self, client: TestClient) -> None:
        assert client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "manual"}).status_code == 200

    def test_without_an_analyst_the_agent_modes_are_refused(self, tmp_path: Path) -> None:
        cfg = make_cfg(db_url=f"sqlite:///{tmp_path / 'nollm.db'}", llm={"provider": "off"})
        app = create_app(lambda: Engine(cfg, Settings(_env_file=None), provider_factory=FixtureProvider))
        with TestClient(app) as c:
            assert engine_of(c).analyst is None
            r = c.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "auto", "confirm": True})
            assert r.status_code == 409 and "analyst" in r.json()["detail"]["message"]


class TestCards:
    def pending_card(self, client: TestClient) -> str:
        client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "copilot"})
        push_tick(client)
        return emit_signal(client)

    def test_a_copilot_signal_appears_as_a_card(self, client: TestClient) -> None:
        sid = self.pending_card(client)
        cards = client.get(f"/api/books/{BOOK_ID}/cards").json()
        assert [c["id"] for c in cards] == [sid]
        card = cards[0]
        assert card["card_status"] == "pending"
        assert card["confidence"] == 88
        assert "Trend intact" in card["reasoning"]
        assert card["seconds_left"] is not None and card["expired"] is False
        assert card["entry_ref"] == "65000" and card["stop_loss"] == "64000"

    def test_approving_a_card_opens_a_position(self, client: TestClient) -> None:
        sid = self.pending_card(client)
        r = client.post(f"/api/signals/{sid}/card", json={"approve": True})
        assert r.status_code == 200 and r.json()["card_status"] == "approved"
        assert r.json()["trade_id"]
        assert client.get(f"/api/books/{BOOK_ID}/cards").json() == []

    def test_rejecting_a_card_shadow_tracks_it(self, client: TestClient) -> None:
        sid = self.pending_card(client)
        r = client.post(f"/api/signals/{sid}/card", json={"approve": False})
        body = r.json()
        assert r.status_code == 200 and body["card_status"] == "rejected"
        assert body["shadow_trade_id"], "a rejected card is still tracked, as learning data"
        book = client.get(f"/api/books/{BOOK_ID}").json()
        assert book["positions"] == [], "the real book is untouched"

    def test_answering_twice_is_refused(self, client: TestClient) -> None:
        sid = self.pending_card(client)
        client.post(f"/api/signals/{sid}/card", json={"approve": True})
        r = client.post(f"/api/signals/{sid}/card", json={"approve": True})
        assert r.status_code == 422 and "already" in r.json()["detail"]["message"]

    def test_an_unknown_card_is_a_422(self, client: TestClient) -> None:
        r = client.post("/api/signals/sig_nope/card", json={"approve": True})
        assert r.status_code == 422


class TestJournal:
    def test_taken_and_skipped_signals_are_both_listed(self, client: TestClient) -> None:
        push_tick(client)
        taken = emit_signal(client)
        client.fake_llm.replies = [SKIP]  # type: ignore[attr-defined]
        skipped = emit_signal(client, id="sig_skipped", entry_ref=D("66000"), stop_loss=D("65000"))
        rows = client.get(f"/api/books/{BOOK_ID}/journal").json()
        by_id = {r["id"]: r for r in rows}
        assert by_id[taken]["decision"] == "take"
        assert by_id[skipped]["decision"] == "skip"
        assert by_id[skipped]["reasoning"] == "Chop."

    def test_the_decision_filter_works(self, client: TestClient) -> None:
        push_tick(client)
        emit_signal(client)
        client.fake_llm.replies = [SKIP]  # type: ignore[attr-defined]
        emit_signal(client, id="sig_s", entry_ref=D("66000"), stop_loss=D("65000"))
        skips = client.get(f"/api/books/{BOOK_ID}/journal", params={"decision": "skip"}).json()
        assert len(skips) == 1 and skips[0]["decision"] == "skip"

    def test_the_strategy_filter_works(self, client: TestClient) -> None:
        push_tick(client)
        emit_signal(client)
        assert client.get(f"/api/books/{BOOK_ID}/journal", params={"strategy_id": "nope"}).json() == []
        assert len(client.get(f"/api/books/{BOOK_ID}/journal", params={"strategy_id": "ema_pullback"}).json()) == 1

    def test_signal_detail_includes_the_llm_call_and_reasoning(self, client: TestClient) -> None:
        client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "auto", "confirm": True})
        push_tick(client)
        sid = emit_signal(client)
        detail = client.get(f"/api/signals/{sid}").json()
        assert detail["signal"]["confidence"] == 88
        assert detail["signal"]["risks_noted"] == ["ADX may roll over"]
        call = detail["llm_call"]
        assert call and call["ok"] is True and call["prompt_version"] == "analyst_v1"
        assert call["provider"] == "fake" and call["latency_ms"] >= 0
        assert detail["trade"] is not None  # auto mode executed it

    def test_an_unknown_signal_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/signals/sig_nope").status_code == 404


class TestLLMEndpoints:
    def test_status_reports_usage_against_the_budget(self, client: TestClient) -> None:
        push_tick(client)
        emit_signal(client)
        st = client.get("/api/llm/status").json()
        assert st["enabled"] is True and st["provider"] == "fake"
        assert st["model"] == "test-model"
        assert st["calls_today"] == 1 and st["daily_budget"] == 50 and st["remaining"] == 49
        assert st["paused"] is False

    def test_the_top_bar_sees_the_same_numbers(self, client: TestClient) -> None:
        push_tick(client)
        emit_signal(client)
        llm = client.get("/api/system/status").json()["llm"]
        assert llm["calls_today"] == 1 and llm["daily_budget"] == 50

    def test_the_call_log_is_available(self, client: TestClient) -> None:
        push_tick(client)
        emit_signal(client)
        calls = client.get("/api/llm/calls").json()
        assert len(calls) == 1
        assert calls[0]["prompt"] and calls[0]["response"]
        assert calls[0]["error_reason"] is None

    def test_an_llm_failure_is_visible_in_the_log(self, client: TestClient) -> None:
        from papermind.llm.base import LLMTimeout

        client.fake_llm.fail_with = LLMTimeout("too slow")  # type: ignore[attr-defined]
        client.put(f"/api/books/{BOOK_ID}/mode", json={"mode": "auto", "confirm": True})
        push_tick(client)
        sid = emit_signal(client)
        calls = client.get("/api/llm/calls").json()
        assert calls[0]["ok"] is False and calls[0]["error_reason"] == "llm_timeout"
        # and nothing was traded
        assert client.get(f"/api/books/{BOOK_ID}").json()["positions"] == []
        assert client.get(f"/api/signals/{sid}").json()["signal"]["decision"] == "skip"


class TestSettings:
    def test_settings_shows_the_model_limits_and_strategies(self, client: TestClient) -> None:
        s = client.get("/api/settings").json()
        assert s["llm"]["model"] == "test-model"
        assert s["llm"]["limits"]["requests_per_day"] == 50
        assert s["llm"]["confidence_threshold"] == 70
        book = s["books"][BOOK_ID]
        assert book["risk"]["max_leverage"] == "3"
        assert book["risk"]["max_risk_per_trade_pct"] == "1"
        assert isinstance(book["strategies"], list)  # the test book configures none
        assert book["instruments"] == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        assert book["copilot"]["card_ttl_bars"] == 1.0
        assert book["shadow_raw_strategy"] is True
        assert book["charges"]["taker_pct"] == "0.05"
        assert s["data"]["stale_after_seconds"] == 10.0

    def test_secrets_are_presence_only(self, client: TestClient) -> None:
        body = client.get("/api/settings").text
        assert "sk-should-never-leak" not in body, "a secret value must never reach the API"
        secrets = client.get("/api/settings").json()["secrets"]
        assert secrets["gemini_api_key"] is True  # set, but the value is not shown
        assert all(isinstance(v, bool) for v in secrets.values())
