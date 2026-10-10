"""OPTIONAL live smoke test against the real Gemini API. Skipped by default.

This is the only test in the suite that can touch the network, and it will not run unless you
ask for it explicitly:

    PAPERMIND_LIVE_LLM=1 uv run pytest tests/llm/test_live_gemini.py -v -m live

It needs GEMINI_API_KEY in `.env`, spends one call of your free-tier daily budget, and exists
to verify one thing the stubs cannot: that the model id in config is real and that the request
shape this code sends is still accepted by the live API.
"""

from __future__ import annotations

import json
import os

import pytest

from papermind.analyst.schema import AnalystVerdict, response_schema
from papermind.core.config import load_config
from papermind.llm.base import LLMRequest
from papermind.llm.gemini import GeminiProvider

LIVE_ENABLED = os.environ.get("PAPERMIND_LIVE_LLM", "").strip() not in ("", "0", "false", "no")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE_ENABLED, reason="live LLM test: set PAPERMIND_LIVE_LLM=1 to run it"),
]


@pytest.mark.asyncio
async def test_the_configured_model_answers_with_valid_json() -> None:
    cfg = load_config().llm
    if not os.environ.get(cfg.api_key_env):
        pytest.skip(f"{cfg.api_key_env} is not set")
    provider = GeminiProvider(cfg)
    schema = response_schema()
    req = LLMRequest(
        system=(
            "You judge one candidate paper trade. Answer with JSON matching the schema and "
            "nothing else. This is a connectivity check, so answer SKIP."
        ),
        prompt="Signal: long BTC/USDT at 65000, stop 64000, target 67000. Regime: trending up.",
        schema=schema,
        model=cfg.model,
        temperature=0.0,
        max_output_tokens=cfg.max_output_tokens,
        thinking_level=cfg.thinking_level,
        timeout_seconds=max(cfg.timeout_seconds, 30.0),
    )
    resp = await provider.complete(req)
    print(f"\nmodel={resp.model} latency={resp.latency_ms}ms tokens={resp.total_tokens}\n{resp.text}")

    verdict = AnalystVerdict.model_validate_json(resp.text.strip().removeprefix("```json").removesuffix("```").strip())
    assert verdict.decision in ("TAKE", "SKIP")
    assert 0 <= verdict.confidence <= 100
    # the model echoed our schema back correctly, which is the whole point of the check
    assert isinstance(json.loads(resp.text.strip().removeprefix("```json").removesuffix("```").strip()), dict)
