"""The analyst's output contract. Anything that fails this validation means NO TRADE.

`suggested_adjustments` is deliberately narrow: the only things the analyst may ask for are a
TIGHTER stop and a SMALLER size. `apply_adjustments` enforces that — a looser value is dropped,
never applied. The risk manager still runs afterwards and has the final say.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from papermind.core.types import Direction


class Adjustments(BaseModel):
    """Stricter-only tweaks. Both are optional; anything else the model invents is ignored."""

    model_config = ConfigDict(extra="ignore")

    stop_loss: Decimal | None = Field(
        default=None, description="A TIGHTER stop only — closer to entry than the strategy's."
    )
    size_factor: float | None = Field(
        default=None, description="Shrink size, 0 < factor <= 1. Values above 1 are ignored."
    )

    @field_validator("size_factor")
    @classmethod
    def _sane_factor(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v <= 0:
            return None  # a zero/negative size is a SKIP, expressed properly by `decision`
        return v


class AnalystVerdict(BaseModel):
    """Strict JSON contract. `extra="ignore"` keeps a chatty model from failing the whole call."""

    model_config = ConfigDict(extra="ignore")

    decision: str = Field(description="TAKE or SKIP")
    confidence: int = Field(ge=0, le=100, description="0-100 confidence in this decision")
    reasoning: str = Field(default="", description="2-3 short sentences of plain English")
    risks_noted: list[str] = Field(default_factory=list)
    lessons_applied: list[str] = Field(default_factory=list)  # Phase 4; empty for now
    suggested_adjustments: Adjustments = Field(default_factory=Adjustments)

    @field_validator("decision", mode="before")
    @classmethod
    def _norm_decision(cls, v: Any) -> str:
        text = str(v or "").strip().upper()
        if text not in ("TAKE", "SKIP"):
            raise ValueError(f"decision must be TAKE or SKIP, got {v!r}")
        return text

    @field_validator("risks_noted", "lessons_applied", mode="before")
    @classmethod
    def _as_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        return []

    @field_validator("reasoning", mode="before")
    @classmethod
    def _as_text(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @property
    def take(self) -> bool:
        return self.decision == "TAKE"

    def json_schema(self) -> dict[str, Any]:  # pragma: no cover - convenience
        return self.model_json_schema()


def response_schema() -> dict[str, Any]:
    """JSON Schema handed to the provider so it constrains its own output."""
    return AnalystVerdict.model_json_schema()


def apply_adjustments(
    direction: Direction, entry_ref: Decimal, stop_loss: Decimal, adj: Adjustments
) -> tuple[Decimal, dict[str, Any]]:
    """Return (stop_loss to use, what was actually applied).

    A suggested stop is accepted only if it is strictly TIGHTER — nearer to the entry on the
    risk side — and still on the correct side of it. Anything else is recorded as rejected.
    """
    applied: dict[str, Any] = {}
    new_stop = stop_loss
    if adj.stop_loss is not None:
        proposed = adj.stop_loss
        tighter = stop_loss < proposed < entry_ref if direction is Direction.LONG else entry_ref < proposed < stop_loss
        if tighter:
            new_stop = proposed
            applied["stop_loss"] = {"from": str(stop_loss), "to": str(proposed)}
        else:
            applied["stop_loss_rejected"] = {
                "proposed": str(proposed),
                "why": "not strictly tighter than the strategy's stop (the analyst may never loosen risk)",
            }
    if adj.size_factor is not None:
        if adj.size_factor < 1:
            applied["size_factor"] = adj.size_factor
        else:
            applied["size_factor_rejected"] = {
                "proposed": adj.size_factor,
                "why": "the analyst may only shrink size, never increase it",
            }
    return new_stop, applied
