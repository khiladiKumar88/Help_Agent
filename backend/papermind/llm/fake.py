"""Deterministic stand-in for the LLM. Every test uses this; no test touches the real Gemini API.

The verdict is derived from a hash of the prompt, so the same prompt always gives the same
answer without any network call. Failure modes (timeout, 429, budget, bad JSON) can be forced,
which is how the fail-safe path is tested.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from papermind.llm.base import (
    LLMError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)

# A verdict the analyst schema accepts, used when no scripted reply is given.
DEFAULT_CONFIDENCE = 80


class FakeProvider(LLMProvider):
    """Scriptable, deterministic provider.

    * `replies`: exact strings returned in order, then the last one repeats.
    * `fail_with`: raise this error instead of answering (forces the NO-TRADE path).
    * `take_if`: decide TAKE/SKIP from the prompt; default is a stable hash of the prompt.
    """

    name = "fake"

    def __init__(
        self,
        replies: list[str] | None = None,
        fail_with: LLMError | None = None,
        take_if: Callable[[str], bool] | None = None,
        confidence: int = DEFAULT_CONFIDENCE,
        reasoning: str = "Setup matches the strategy's conditions and the regime agrees.",
        latency_ms: int = 7,
    ) -> None:
        self.replies = list(replies or [])
        self.fail_with = fail_with
        self.take_if = take_if
        self.confidence = confidence
        self.reasoning = reasoning
        self.latency_ms = latency_ms
        self.calls: list[LLMRequest] = []

    def _verdict(self, req: LLMRequest) -> str:
        if self.take_if is not None:
            take = self.take_if(req.prompt)
        else:
            digest = hashlib.sha256(req.prompt.encode("utf-8")).digest()
            take = digest[0] % 2 == 0
        return json.dumps(
            {
                "decision": "TAKE" if take else "SKIP",
                "confidence": self.confidence if take else max(0, min(self.confidence, 40)),
                "reasoning": self.reasoning if take else "Context does not support this setup right now.",
                "risks_noted": ["fake provider — no real analysis"],
                "lessons_applied": [],
                "suggested_adjustments": {},
            }
        )

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        if self.fail_with is not None:
            raise self.fail_with
        text = (
            self.replies.pop(0) if len(self.replies) > 1 else (self.replies[0] if self.replies else self._verdict(req))
        )
        return LLMResponse(
            text=text,
            model=req.model,
            input_tokens=len(req.prompt) // 4,
            output_tokens=len(text) // 4,
            total_tokens=(len(req.prompt) + len(text)) // 4,
            latency_ms=self.latency_ms,
        )
