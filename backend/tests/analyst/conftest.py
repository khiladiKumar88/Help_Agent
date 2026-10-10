"""Analyst test rig: a real journal + registry on in-memory SQLite, with a FakeProvider.

No test here touches the network. The one live smoke test lives in tests/llm/test_live_gemini.py
and is skipped unless it is explicitly enabled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from papermind.analyst.analyst import LLMAnalyst
from papermind.core.config import AppConfig, LLMConfig, LLMRateLimits
from papermind.core.ids import new_id
from papermind.core.types import Direction, Signal
from papermind.llm.base import LLMProvider
from papermind.llm.fake import FakeProvider
from papermind.llm.limiter import RateLimiter
from tests.conftest import BOOK_ID, BTC, Env, build_env, make_cfg

D = Decimal


LIMIT_KEYS = frozenset(LLMRateLimits.model_fields)


def llm_cfg(**over: Any) -> dict[str, Any]:
    """Build an llm config block; keys belonging to LLMRateLimits are routed into `limits`."""
    limits: dict[str, Any] = {"requests_per_minute": 600, "requests_per_day": 100, "tokens_per_minute": None}
    base: dict[str, Any] = {
        "provider": "fake",
        "model": "test-model",
        "confidence_threshold": 70,
        "cache_responses": True,
    }
    for k, v in over.items():
        (limits if k in LIMIT_KEYS else base)[k] = v
    base["limits"] = limits
    return base


def cfg_with_llm(**over: Any) -> AppConfig:
    return make_cfg(llm=llm_cfg(**over))


@dataclass
class Rig:
    env: Env
    analyst: LLMAnalyst
    provider: LLMProvider

    @property
    def journal(self) -> Any:
        return self.env.journal

    def signal(self, **over: Any) -> Signal:
        """A long BTC signal: entry 65000, stop 64000, target 67000."""
        fields: dict[str, Any] = {
            "id": new_id("sig"),
            "ts": self.env.clock.now(),
            "book_id": BOOK_ID,
            "instrument_id": BTC,
            "strategy_id": "ema_pullback",
            "params": {"adx_min": 20, "rr": 2.0},
            "timeframe": "15m",
            "direction": Direction.LONG,
            "entry_ref": D("65000"),
            "stop_loss": D("64000"),
            "target": D("67000"),
            "setup": "pullback to EMA21 in an uptrend",
            "regime": {"label": "trending_up", "adx": 31.2, "time_bucket": "europe", "expiry_day": False},
            "context": {"close": 65010.0, "ema21": 64800.0, "rsi14": 58.1, "adx14": 31.2, "atr14": 240.0},
        }
        fields.update(over)
        sig = Signal.model_validate(fields)
        self.env.journal.save_signal(sig)
        return sig

    def row(self, signal_id: str) -> dict[str, Any]:
        row = self.env.journal.get_signal(signal_id)
        assert row is not None
        return row

    def calls(self) -> list[dict[str, Any]]:
        return self.env.journal.list_llm_calls(limit=100)


def build_rig(provider: LLMProvider | None = None, cfg: AppConfig | None = None, **over: Any) -> Rig:
    cfg = cfg or cfg_with_llm(**over)
    env = build_env(cfg)
    prov = provider or FakeProvider()
    limiter = RateLimiter(
        cfg.llm.limits,
        env.clock,
        store=env.journal,
        monotonic=lambda: 0.0,
        sleep=_no_sleep,  # type: ignore[arg-type]
    )
    analyst = LLMAnalyst(cfg, env.clock, env.journal, env.registry, prov, limiter)
    return Rig(env=env, analyst=analyst, provider=prov)


async def _no_sleep(_seconds: float) -> None:
    return None


def reply(decision: str = "TAKE", confidence: int = 85, **over: Any) -> str:
    """A valid provider reply, so tests can vary one field at a time."""
    payload: dict[str, Any] = {
        "decision": decision,
        "confidence": confidence,
        "reasoning": "Trend is intact and the pullback held the EMA.",
        "risks_noted": ["ADX could roll over"],
        "lessons_applied": [],
        "suggested_adjustments": {},
    }
    payload.update(over)
    return json.dumps(payload)


@pytest.fixture
def rig() -> Rig:
    return build_rig()


@pytest.fixture
def strict_limits() -> LLMRateLimits:
    return LLMRateLimits(requests_per_minute=600, requests_per_day=1, tokens_per_minute=None)


@pytest.fixture
def fake_llm_config() -> LLMConfig:
    return LLMConfig.model_validate(llm_cfg())
