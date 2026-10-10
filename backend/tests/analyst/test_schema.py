"""The analyst's output contract, and the rule that it can only ever make an order STRICTER."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from papermind.analyst.schema import Adjustments, AnalystVerdict, apply_adjustments, response_schema
from papermind.core.types import Direction

D = Decimal


def verdict(**over: object) -> AnalystVerdict:
    base: dict[str, object] = {"decision": "TAKE", "confidence": 80, "reasoning": "ok"}
    base.update(over)
    return AnalystVerdict.model_validate(base)


class TestVerdict:
    def test_parses_a_well_formed_answer(self) -> None:
        v = verdict(risks_noted=["thin volume"], suggested_adjustments={"size_factor": 0.5})
        assert v.take and v.confidence == 80 and v.risks_noted == ["thin volume"]
        assert v.suggested_adjustments.size_factor == 0.5

    @pytest.mark.parametrize("raw", ["take", " Take ", "TAKE"])
    def test_decision_is_case_and_space_insensitive(self, raw: str) -> None:
        assert verdict(decision=raw).take is True

    @pytest.mark.parametrize("raw", ["skip", "SKIP"])
    def test_skip_is_not_take(self, raw: str) -> None:
        assert verdict(decision=raw).take is False

    @pytest.mark.parametrize("raw", ["MAYBE", "", None, "BUY", 1])
    def test_anything_but_take_or_skip_is_rejected(self, raw: object) -> None:
        with pytest.raises(ValidationError):
            verdict(decision=raw)

    @pytest.mark.parametrize("c", [-1, 101, 1000])
    def test_confidence_must_be_0_to_100(self, c: int) -> None:
        with pytest.raises(ValidationError):
            verdict(confidence=c)

    def test_missing_confidence_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnalystVerdict.model_validate({"decision": "TAKE"})

    def test_a_chatty_model_does_not_break_the_call(self) -> None:
        v = AnalystVerdict.model_validate(
            {"decision": "SKIP", "confidence": 10, "chatty_extra": "hello", "reasoning": None}
        )
        assert v.take is False and v.reasoning == ""

    def test_a_string_instead_of_a_list_is_coerced(self) -> None:
        v = verdict(risks_noted="one risk", lessons_applied=None)
        assert v.risks_noted == ["one risk"] and v.lessons_applied == []

    def test_lessons_applied_defaults_empty_for_now(self) -> None:
        assert verdict().lessons_applied == []

    def test_json_round_trip(self) -> None:
        payload = json.dumps({"decision": "TAKE", "confidence": 71, "reasoning": "trend intact"})
        assert AnalystVerdict.model_validate_json(payload).confidence == 71

    def test_schema_is_json_serialisable_for_the_provider(self) -> None:
        schema = response_schema()
        assert json.loads(json.dumps(schema))["type"] == "object"
        assert "decision" in schema["properties"] and "confidence" in schema["properties"]


class TestStricterOnly:
    """A long entering at 100 with a stop at 95; a short entering at 100 with a stop at 105."""

    def test_a_tighter_long_stop_is_applied(self) -> None:
        stop, applied = apply_adjustments(Direction.LONG, D("100"), D("95"), Adjustments(stop_loss=D("98")))
        assert stop == D("98")
        assert applied["stop_loss"] == {"from": "95", "to": "98"}

    def test_a_looser_long_stop_is_refused(self) -> None:
        stop, applied = apply_adjustments(Direction.LONG, D("100"), D("95"), Adjustments(stop_loss=D("90")))
        assert stop == D("95")  # unchanged
        assert "stop_loss" not in applied and "stop_loss_rejected" in applied

    def test_a_long_stop_above_the_entry_is_refused(self) -> None:
        stop, applied = apply_adjustments(Direction.LONG, D("100"), D("95"), Adjustments(stop_loss=D("101")))
        assert stop == D("95") and "stop_loss_rejected" in applied

    def test_a_tighter_short_stop_is_applied(self) -> None:
        stop, applied = apply_adjustments(Direction.SHORT, D("100"), D("105"), Adjustments(stop_loss=D("102")))
        assert stop == D("102") and applied["stop_loss"]["to"] == "102"

    def test_a_looser_short_stop_is_refused(self) -> None:
        stop, applied = apply_adjustments(Direction.SHORT, D("100"), D("105"), Adjustments(stop_loss=D("110")))
        assert stop == D("105") and "stop_loss_rejected" in applied

    def test_a_short_stop_below_the_entry_is_refused(self) -> None:
        stop, applied = apply_adjustments(Direction.SHORT, D("100"), D("105"), Adjustments(stop_loss=D("99")))
        assert stop == D("105") and "stop_loss_rejected" in applied

    def test_the_same_stop_is_not_tighter(self) -> None:
        stop, applied = apply_adjustments(Direction.LONG, D("100"), D("95"), Adjustments(stop_loss=D("95")))
        assert stop == D("95") and "stop_loss_rejected" in applied

    def test_a_shrink_factor_is_applied(self) -> None:
        _, applied = apply_adjustments(Direction.LONG, D("100"), D("95"), Adjustments(size_factor=0.25))
        assert applied["size_factor"] == 0.25

    @pytest.mark.parametrize("factor", [1.0, 1.5, 10.0])
    def test_a_factor_of_one_or_more_cannot_grow_the_position(self, factor: float) -> None:
        _, applied = apply_adjustments(Direction.LONG, D("100"), D("95"), Adjustments(size_factor=factor))
        assert "size_factor" not in applied and "size_factor_rejected" in applied

    @pytest.mark.parametrize("factor", [0.0, -1.0])
    def test_a_zero_or_negative_factor_is_dropped(self, factor: float) -> None:
        # expressed as a SKIP via `decision`, never as a zero-size order
        assert Adjustments(size_factor=factor).size_factor is None

    def test_no_adjustments_changes_nothing(self) -> None:
        stop, applied = apply_adjustments(Direction.LONG, D("100"), D("95"), Adjustments())
        assert stop == D("95") and applied == {}
