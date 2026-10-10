"""LLMProvider interface plus the errors that always mean NO TRADE.

Every provider is swappable by config (`llm.provider`). Nothing here knows about trading;
the analyst layer owns prompts and validation.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(Exception):
    """Base for every LLM failure. The analyst turns all of these into SKIP (no trade)."""

    reason = "llm_error"


class LLMTimeout(LLMError):
    reason = "llm_timeout"


class LLMRateLimited(LLMError):
    """Provider returned 429, or our own token bucket had nothing left in time."""

    reason = "llm_rate_limited"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMBudgetExhausted(LLMError):
    """The daily call budget is used up. The agent pauses rather than trading blind."""

    reason = "llm_budget_exhausted"


class LLMInvalidOutput(LLMError):
    """Response was missing, empty, not JSON, or failed schema validation."""

    reason = "llm_invalid_output"


class LLMNotConfigured(LLMError):
    reason = "llm_not_configured"


@dataclass(frozen=True)
class LLMRequest:
    """One structured-JSON call. `schema` is a JSON Schema the provider asks the model to obey."""

    system: str
    prompt: str
    schema: dict[str, Any]
    model: str
    temperature: float
    max_output_tokens: int
    thinking_level: str
    timeout_seconds: float

    def cache_key(self) -> str:
        """Stable hash of everything that can change the answer — used to cache replays."""
        h = hashlib.sha256()
        for part in (
            self.model,
            self.system,
            self.prompt,
            repr(sorted(self.schema.items())),
            f"{self.temperature}",
            f"{self.max_output_tokens}",
            self.thinking_level,
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()


@dataclass
class LLMResponse:
    text: str  # raw model output (expected to be JSON)
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int = 0
    cached: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """Swap by config. Implementations must raise an LLMError subclass for every failure."""

    name: str = "base"

    @abstractmethod
    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Return the model's raw text, or raise an LLMError. Must never return None."""

    async def close(self) -> None:
        return None
