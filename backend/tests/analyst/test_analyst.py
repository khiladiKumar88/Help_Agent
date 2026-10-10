"""The analyst's verdicts and — above all — that every failure mode means NO TRADE."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from papermind.llm.base import (
    LLMBudgetExhausted,
    LLMError,
    LLMInvalidOutput,
    LLMRateLimited,
    LLMTimeout,
)
from papermind.llm.fake import FakeProvider
from tests.analyst.conftest import Rig, build_rig, reply

D = Decimal


class TestVerdicts:
    async def test_take_above_the_threshold_is_taken(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 85)]  # type: ignore[attr-defined]
        d = await rig.analyst.decide(rig.signal())
        assert d.take is True and d.by == "llm" and d.confidence == 85
        assert "Trend is intact" in d.reason

    async def test_skip_is_not_taken(self, rig: Rig) -> None:
        rig.provider.replies = [reply("SKIP", 90)]  # type: ignore[attr-defined]
        d = await rig.analyst.decide(rig.signal())
        assert d.take is False and d.confidence == 90

    async def test_take_below_the_confidence_threshold_becomes_a_skip(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 69)]  # threshold is 70
        d = await rig.analyst.decide(rig.signal())
        assert d.take is False
        assert "below the 70 threshold" in d.reason

    async def test_exactly_at_the_threshold_is_taken(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 70)]
        assert (await rig.analyst.decide(rig.signal())).take is True

    async def test_the_threshold_comes_from_config(self) -> None:
        rig = build_rig(confidence_threshold=95)
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        assert (await rig.analyst.decide(rig.signal())).take is False

    async def test_the_verdict_is_stored_on_the_signal(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 77, risks_noted=["wide spread"])]
        sig = rig.signal()
        await rig.analyst.decide(sig)
        row = rig.row(sig.id)
        assert row["confidence"] == 77
        assert row["risks_noted"] == ["wide spread"]
        assert row["prompt_version"] == "analyst_v1"
        assert row["llm_call_id"] and row["llm_call_id"].startswith("llm")


class TestFailSafe:
    """Stale data, LLM error, rate limit, bad JSON, any exception => NO TRADE."""

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (LLMTimeout("too slow"), "llm_timeout"),
            (LLMRateLimited("429 from provider"), "llm_rate_limited"),
            (LLMBudgetExhausted("out of budget"), "llm_budget_exhausted"),
            (LLMInvalidOutput("empty"), "llm_invalid_output"),
            (LLMError("something else"), "llm_error"),
        ],
    )
    async def test_every_provider_failure_is_no_trade(self, error: LLMError, reason: str) -> None:
        rig = build_rig(FakeProvider(fail_with=error))
        sig = rig.signal()
        d = await rig.analyst.decide(sig)
        assert d.take is False, f"{reason} must never produce a trade"
        assert reason in d.reason and d.reason.startswith("NO TRADE")
        call = rig.calls()[0]
        assert call["ok"] is False and call["error_reason"] == reason

    async def test_an_unexpected_exception_is_no_trade(self) -> None:
        class Exploding(FakeProvider):
            async def complete(self, req: object) -> object:  # type: ignore[override]
                raise RuntimeError("boom")

        rig = build_rig(Exploding())
        d = await rig.analyst.decide(rig.signal())
        assert d.take is False and "boom" in d.reason

    @pytest.mark.parametrize(
        "bad",
        [
            "not json at all",
            "",
            "{}",
            '{"decision": "MAYBE", "confidence": 80}',
            '{"decision": "TAKE"}',
            '{"decision": "TAKE", "confidence": 150}',
            '{"decision": "TAKE", "confidence": "high"}',
            "[1, 2, 3]",
        ],
    )
    async def test_invalid_json_or_schema_is_no_trade(self, bad: str) -> None:
        rig = build_rig(FakeProvider(replies=[bad, bad]))
        d = await rig.analyst.decide(rig.signal())
        assert d.take is False
        assert "llm_invalid_output" in d.reason

    async def test_a_fenced_code_block_is_still_parsed(self, rig: Rig) -> None:
        rig.provider.replies = ["```json\n" + reply("TAKE", 88) + "\n```"]  # type: ignore[attr-defined]
        assert (await rig.analyst.decide(rig.signal())).take is True

    async def test_a_failure_is_recorded_against_the_signal(self) -> None:
        rig = build_rig(FakeProvider(fail_with=LLMTimeout("too slow")))
        sig = rig.signal()
        await rig.analyst.decide(sig)
        row = rig.row(sig.id)
        assert row["reasoning"].startswith("NO TRADE")
        assert row["confidence"] is None  # no verdict was produced

    async def test_an_exhausted_budget_pauses_the_analyst(self) -> None:
        rig = build_rig(requests_per_day=1)
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        assert (await rig.analyst.decide(rig.signal())).take is True
        assert rig.analyst.paused is True
        # a DIFFERENT signal needs a fresh call, and there is no budget left for one
        d = await rig.analyst.decide(rig.signal(id="sig_second", entry_ref=D("70000"), stop_loss=D("69000")))
        assert d.take is False and "budget" in d.reason
        assert rig.analyst.status()["paused"] is True

    async def test_a_cached_verdict_is_still_honoured_once_the_budget_is_gone(self) -> None:
        """Deliberate: a cached answer is a real verdict we already paid for, so it is not
        'trading blind'. It costs no quota, so an exhausted budget must not discard it."""
        rig = build_rig(requests_per_day=1)
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        first = await rig.analyst.decide(rig.signal(id="sig_a"))
        assert first.take is True and rig.analyst.paused is True
        # same context => same prompt hash => served from cache, no new call
        again = await rig.analyst.decide(rig.signal(id="sig_b"))
        assert again.take is True and again.cached is True
        assert len(rig.provider.calls) == 1  # type: ignore[attr-defined]


class TestCallLogging:
    async def test_a_successful_call_is_logged_in_full(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 80)]  # type: ignore[attr-defined]
        sig = rig.signal()
        await rig.analyst.decide(sig)
        call = rig.calls()[0]
        assert call["signal_id"] == sig.id and call["book_id"] == sig.book_id
        assert call["ok"] is True and call["purpose"] == "analyst"
        assert call["provider"] == "fake" and call["model"] == "test-model"
        assert call["prompt_version"] == "analyst_v1" and len(call["prompt_hash"]) == 64
        assert call["prompt"] and call["system_prompt"]
        assert json.loads(call["response"])["decision"] == "TAKE"
        assert call["input_tokens"] and call["output_tokens"]
        assert call["latency_ms"] >= 0

    async def test_prompts_can_be_withheld_from_the_log(self) -> None:
        rig = build_rig(log_prompts=False)
        rig.provider.replies = [reply()]  # type: ignore[attr-defined]
        await rig.analyst.decide(rig.signal())
        call = rig.calls()[0]
        assert call["prompt"] == "" and call["system_prompt"] == ""
        assert call["response"]  # the answer is still kept

    async def test_no_secret_ever_reaches_a_prompt(self, rig: Rig) -> None:
        rig.provider.replies = [reply()]  # type: ignore[attr-defined]
        await rig.analyst.decide(rig.signal())
        req = rig.provider.calls[0]  # type: ignore[attr-defined]
        blob = (req.system + req.prompt).lower()
        for forbidden in ("api_key", "apikey", "gemini_api_key", "secret", "password", "bearer ", "token="):
            assert forbidden not in blob, f"{forbidden!r} must never appear in a prompt"


class TestCache:
    async def test_an_identical_prompt_is_answered_from_cache(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 81)]  # type: ignore[attr-defined]
        first = rig.signal(id="sig_a")
        await rig.analyst.decide(first)
        calls_before = len(rig.provider.calls)  # type: ignore[attr-defined]
        used_before = rig.analyst.limiter.usage.calls

        # same context => same prompt => same hash
        second = rig.signal(id="sig_b")
        d = await rig.analyst.decide(second)
        assert d.take is True and d.cached is True
        assert len(rig.provider.calls) == calls_before, "the provider must not be called again"  # type: ignore[attr-defined]
        assert rig.analyst.limiter.usage.calls == used_before, "a cached answer costs no quota"
        assert rig.analyst.limiter.usage.cached_hits == 1

    async def test_a_different_signal_is_not_served_from_cache(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 81), reply("SKIP", 90)]  # type: ignore[attr-defined]
        await rig.analyst.decide(rig.signal(id="sig_a"))
        d = await rig.analyst.decide(rig.signal(id="sig_b", entry_ref=D("70000"), stop_loss=D("69000")))
        assert len(rig.provider.calls) == 2  # type: ignore[attr-defined]
        assert d.cached is False

    async def test_caching_can_be_switched_off(self) -> None:
        rig = build_rig(cache_responses=False)
        rig.provider.replies = [reply("TAKE", 81), reply("TAKE", 81)]  # type: ignore[attr-defined]
        await rig.analyst.decide(rig.signal(id="sig_a"))
        await rig.analyst.decide(rig.signal(id="sig_b"))
        assert len(rig.provider.calls) == 2  # type: ignore[attr-defined]


class TestStricterOnly:
    async def test_a_tighter_stop_is_passed_on(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 85, suggested_adjustments={"stop_loss": "64500"})]
        sig = rig.signal()
        d = await rig.analyst.decide(sig)
        assert d.stop_loss == D("64500")
        assert rig.row(sig.id)["adjustments"]["stop_loss"]["to"] == "64500"

    async def test_a_looser_stop_is_discarded(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 85, suggested_adjustments={"stop_loss": "60000"})]
        sig = rig.signal()
        d = await rig.analyst.decide(sig)
        assert d.stop_loss is None, "a looser stop must never reach the broker"
        assert "stop_loss_rejected" in rig.row(sig.id)["adjustments"]

    async def test_a_shrink_factor_is_passed_on(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 85, suggested_adjustments={"size_factor": 0.5})]
        d = await rig.analyst.decide(rig.signal())
        assert d.size_factor == 0.5

    async def test_a_bigger_size_is_discarded(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 85, suggested_adjustments={"size_factor": 3})]
        sig = rig.signal()
        d = await rig.analyst.decide(sig)
        assert d.size_factor is None
        assert "size_factor_rejected" in rig.row(sig.id)["adjustments"]


class TestStatus:
    async def test_status_reports_what_the_top_bar_needs(self, rig: Rig) -> None:
        rig.provider.replies = [reply()]  # type: ignore[attr-defined]
        await rig.analyst.decide(rig.signal())
        st = rig.analyst.status()
        assert st["provider"] == "fake" and st["model"] == "test-model"
        assert st["calls"] == 1 and st["daily_budget"] == 100 and st["remaining"] == 99
        assert st["confidence_threshold"] == 70 and st["paused"] is False
        assert st["prompt_version"] == "analyst_v1"
