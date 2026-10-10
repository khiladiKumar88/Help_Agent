"""Backtests and replays with the analyst in the loop: modes, caching, quota, hindsight label.

Only the `fake` mode is exercised, which is deterministic and offline. `on` is covered by the
analyst unit tests plus the opt-in live smoke test.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import papermind.backtest.runner as runner
from papermind.backtest.runner import (
    BacktestError,
    BacktestSpec,
    ReplaySpec,
    llm_report,
    run_backtest,
    run_replay,
)
from papermind.core.clock import ReplayClock
from papermind.data.history import HistoryDownloader, HistoryStore, SimulatedHistoryExchange
from papermind.llm.fake import FakeProvider
from tests.conftest import BOOK_ID, book_dict, make_cfg

END = datetime(2026, 3, 1, tzinfo=UTC)
SYM = "BTC/USDT:USDT"

SKIP_ALL = json.dumps(
    {"decision": "SKIP", "confidence": 95, "reasoning": "fake: skip", "risks_noted": [], "lessons_applied": []}
)


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> HistoryStore:
    path: Path = tmp_path_factory.mktemp("hist_llm") / "h.db"
    st = HistoryStore(f"sqlite:///{path}")
    clock = ReplayClock(END)

    async def load() -> None:
        dl = HistoryDownloader(st, lambda _id: SimulatedHistoryExchange(clock), clock)
        await dl.sync("simulated", SYM, "15m", END - timedelta(days=20))
        await dl.sync("simulated", SYM, "5m", END - timedelta(days=5))

    asyncio.run(load())
    return st


def cfg_with(llm: dict[str, Any] | None = None, **book: Any) -> Any:
    return make_cfg(
        {**book_dict(), **book},
        llm=llm or {"provider": "fake", "model": "fake-model", "limits": {"requests_per_day": 10_000}},
    )


def spec(**kw: Any) -> BacktestSpec:
    base: dict[str, Any] = {
        "book_id": BOOK_ID,
        "exchange": "simulated",
        "symbol": SYM,
        "base_tf": "15m",
        "start": END - timedelta(days=7),
        "end": END,
        "strategy_id": "supertrend_flip",
    }
    base.update(kw)
    return BacktestSpec(**base)


class TestLLMModes:
    def test_off_is_the_default(self) -> None:
        assert spec().llm == "off"
        assert ReplaySpec(book_id=BOOK_ID, exchange="simulated", symbol=SYM, day=END).llm == "off"

    async def test_off_takes_every_signal(self, store: HistoryStore) -> None:
        r = await run_backtest(cfg_with(), store, spec(llm="off"))
        assert r["llm"]["mode"] == "off"
        assert r["llm"]["hindsight_risk"] is False and r["llm"]["hindsight_warning"] is None
        assert r["signals"] > 0

    async def test_fake_mode_runs_the_analyst_offline(self, store: HistoryStore) -> None:
        r = await run_backtest(cfg_with(), store, spec(llm="fake"))
        assert r["llm"]["mode"] == "fake"
        assert "offline" in r["llm"]["description"]
        assert r["llm"]["hindsight_risk"] is False  # a stand-in cannot have seen the data

    async def test_the_analyst_filter_changes_the_trade_count(
        self, store: HistoryStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the filter: skipping signals must actually skip trades."""
        off = await run_backtest(cfg_with(), store, spec(llm="off"))
        assert off["agent"]["trades"] > 0

        # make the offline stand-in refuse everything
        monkeypatch.setattr(runner, "FakeProvider", lambda: FakeProvider(replies=[SKIP_ALL, SKIP_ALL]))
        skipping = await run_backtest(cfg_with(), store, spec(llm="fake"))
        assert skipping["agent"]["trades"] == 0, "an analyst that skips everything must trade nothing"
        assert skipping["signals"] == off["signals"], "the same signals were still generated and recorded"

    async def test_asking_for_the_llm_when_it_is_unavailable_fails_loudly(self, store: HistoryStore) -> None:
        cfg = cfg_with(llm={"provider": "off"})
        with pytest.raises(BacktestError, match="unavailable"):
            await run_backtest(cfg, store, spec(llm="on"))


class TestHindsightLabel:
    def test_on_is_labelled_as_possible_hindsight_bias(self) -> None:
        rep = llm_report("on")
        assert rep["hindsight_risk"] is True
        assert "trained" in rep["hindsight_warning"]
        assert "not" in rep["hindsight_warning"].lower()

    @pytest.mark.parametrize("mode", ["off", "fake"])
    def test_offline_modes_carry_no_hindsight_warning(self, mode: str) -> None:
        rep = llm_report(mode)  # type: ignore[arg-type]
        assert rep["hindsight_risk"] is False and rep["hindsight_warning"] is None

    def test_every_mode_is_described_in_plain_english(self) -> None:
        for mode in ("off", "fake", "on"):
            assert llm_report(mode)["description"]  # type: ignore[arg-type]


class TestQuotaAndCache:
    async def test_a_replay_with_the_analyst_records_reasoning_on_its_signals(self, store: HistoryStore) -> None:
        cfg = cfg_with(strategies=[{"id": "supertrend_flip"}])
        day = END - timedelta(days=2)
        r = await run_replay(
            cfg,
            store,
            ReplaySpec(book_id=BOOK_ID, exchange="simulated", symbol=SYM, day=day, base_tf="5m", llm="fake"),
        )
        assert r["llm"]["mode"] == "fake" and r["llm_mode"] == "fake"
        assert r["signal_list"], "the replay should produce signal cards"
        for s in r["signal_list"]:
            assert s["decision"] in ("take", "skip")

    async def test_the_daily_budget_applies_to_replays_too(self, store: HistoryStore) -> None:
        """A tiny budget must stop the analyst deciding, and so stop it trading.

        The budget is keyed to the REAL day, not the simulated one — otherwise a multi-day
        backtest would reset it once per simulated day and fire many times the daily limit of
        real API calls.
        """
        cfg = cfg_with(
            llm={"provider": "fake", "model": "fake-model", "limits": {"requests_per_day": 1}},
            strategies=[{"id": "supertrend_flip"}],
        )
        r = await run_backtest(cfg, store, spec(llm="fake"))
        # one verdict for the whole 7-day run; every later signal is NO TRADE
        assert r["agent"]["trades"] <= 1
        assert r["signals"] > 1, "signals were still generated and recorded"
