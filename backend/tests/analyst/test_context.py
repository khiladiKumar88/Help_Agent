"""The versioned prompt template and the compact context built from a signal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from papermind.analyst.context import (
    NoEpisodes,
    NoLessons,
    build_prompt,
    load_template,
    market_section,
    regime_section,
    signal_section,
)
from papermind.analyst.schema import response_schema
from papermind.core.types import Signal
from tests.analyst.conftest import build_rig

D = Decimal


@pytest.fixture
def sig() -> Signal:
    return build_rig().signal()


class TestTemplate:
    def test_the_versioned_template_loads_and_splits(self) -> None:
        tpl = load_template("analyst_v1")
        assert tpl.version == "analyst_v1"
        assert "risk-aware analyst" in tpl.system
        assert "{signal}" in tpl.user and "{schema}" in tpl.user

    def test_the_front_matter_is_stripped(self) -> None:
        tpl = load_template("analyst_v1")
        assert "version: analyst_v1" not in tpl.system
        assert not tpl.system.startswith("---")

    def test_the_system_prompt_states_the_hard_limits(self) -> None:
        system = load_template("analyst_v1").system.lower()
        assert "paper" in system
        assert "stricter" in system or "never loosen" in system
        assert "take or skip" in system

    def test_an_unknown_version_fails_loudly(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_template("no_such_template_v9")


class TestSections:
    def test_the_signal_section_carries_the_levels_and_reward_risk(self, sig: Signal) -> None:
        text = signal_section(sig, "USDT", "BTC/USDT:USDT")
        assert "BTC/USDT:USDT" in text and "ema_pullback" in text
        assert "65000" in text and "64000" in text and "67000" in text
        assert "reward:risk 2.00:1" in text
        assert "pullback to EMA21" in text
        assert "adx_min=20" in text

    def test_a_signal_without_a_target_says_none(self, sig: Signal) -> None:
        text = signal_section(sig.model_copy(update={"target": None}), "USDT", "BTC")
        assert "target: none" in text and "reward:risk" not in text

    def test_the_market_section_lists_the_indicators_present(self, sig: Signal) -> None:
        text = market_section(sig)
        assert "RSI14" in text and "ADX14" in text and "EMA21" in text
        assert "VWAP" not in text  # not in this snapshot, so not invented

    def test_missing_indicators_do_not_break_the_prompt(self, sig: Signal) -> None:
        text = market_section(sig.model_copy(update={"context": {}}))
        assert "No indicator snapshot" in text

    def test_the_regime_section_includes_the_time_bucket(self, sig: Signal) -> None:
        text = regime_section(sig)
        assert "trending_up" in text and "europe" in text
        assert "expiry day: no" in text

    def test_a_missing_regime_is_stated_plainly(self, sig: Signal) -> None:
        assert "not available" in regime_section(sig.model_copy(update={"regime": {}}))


class TestBuildPrompt:
    def test_every_placeholder_is_filled(self, sig: Signal) -> None:
        system, user = build_prompt(
            sig, version="analyst_v1", currency="USDT", symbol="BTC/USDT:USDT", schema=response_schema()
        )
        assert system and user
        for placeholder in ("{signal}", "{market}", "{regime}", "{lessons}", "{episodes}", "{schema}"):
            assert placeholder not in user, f"{placeholder} was left unfilled"

    def test_the_schema_is_embedded_so_the_model_knows_the_shape(self, sig: Signal) -> None:
        _, user = build_prompt(sig, version="analyst_v1", currency="USDT", symbol="BTC", schema=response_schema())
        assert '"decision"' in user and '"confidence"' in user

    def test_the_phase_4_hooks_are_called_and_render_empty(self, sig: Signal) -> None:
        _, user = build_prompt(
            sig,
            version="analyst_v1",
            currency="USDT",
            symbol="BTC",
            schema=response_schema(),
            lessons=NoLessons(),
            episodes=NoEpisodes(),
        )
        assert "None yet" in user  # both placeholders say so rather than being blank

    def test_a_lesson_source_is_rendered_when_one_exists(self, sig: Signal) -> None:
        class Lessons:
            def lessons_for(self, signal: Signal) -> list[str]:
                return ["Skip EMA pullbacks when ADX is falling"]

        class Episodes:
            def similar_trades(self, signal: Signal, k: int = 5) -> list[str]:
                return ["2026-05-02 long, +1.4R"]

        _, user = build_prompt(
            sig,
            version="analyst_v1",
            currency="USDT",
            symbol="BTC",
            schema=response_schema(),
            lessons=Lessons(),
            episodes=Episodes(),
        )
        assert "- Skip EMA pullbacks when ADX is falling" in user
        assert "- 2026-05-02 long, +1.4R" in user

    def test_the_prompt_stays_compact(self, sig: Signal) -> None:
        """No raw candle dump: the free tier's token budget is the constraint."""
        _, user = build_prompt(sig, version="analyst_v1", currency="USDT", symbol="BTC", schema=response_schema())
        assert len(user) < 6000, f"prompt grew to {len(user)} chars"
