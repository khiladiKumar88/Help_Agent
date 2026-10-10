"""Gemini provider via the official `google-genai` SDK (Interactions API).

Public, documented surface only — verified against the installed SDK (2.26):
  client.aio.interactions.create(model=, input=, system_instruction=, generation_config=,
                                 response_format=, timeout=) -> Interaction
  Interaction: .output_text, .status, .errors, .usage.total_{input,output,thought}_tokens

The model id and every limit come from `llm` config — nothing is hardcoded here. The API key is
read from the environment (populated from `.env`) and never appears in a prompt or a log.
Retries use exponential backoff on 429 and 5xx; everything else fails fast, and the analyst
turns any failure into NO TRADE.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any

from papermind.core.config import LLMConfig
from papermind.llm.base import (
    LLMError,
    LLMInvalidOutput,
    LLMNotConfigured,
    LLMProvider,
    LLMRateLimited,
    LLMRequest,
    LLMResponse,
    LLMTimeout,
)

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, cfg: LLMConfig, api_key: str | None = None, client: Any | None = None) -> None:
        """`client` is for tests: pass a stub to avoid constructing a real SDK client."""
        self.cfg = cfg
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get(cfg.api_key_env) or ""
        if not key.strip():
            raise LLMNotConfigured(f"{cfg.api_key_env} is not set — add it to .env to enable the Gemini analyst")
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMNotConfigured("google-genai is not installed") from exc
        self._client = genai.Client(api_key=key)

    async def complete(self, req: LLMRequest) -> LLMResponse:
        attempts = max(1, self.cfg.limits.max_retries + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                return await self._once(req, started)
            except LLMRateLimited as exc:
                last = exc
                if attempt == attempts - 1:
                    raise
                await self._backoff(attempt, exc.retry_after)
            except _Retryable as exc:
                cause = exc.__cause__
                last = cause if isinstance(cause, Exception) else exc
                if attempt == attempts - 1:
                    raise LLMError(str(last)) from last
                await self._backoff(attempt, None)
        raise LLMError(str(last) if last else "LLM call failed")

    async def _backoff(self, attempt: int, retry_after: float | None) -> None:
        base = self.cfg.limits.backoff_base_seconds * (2**attempt)
        delay = min(retry_after if retry_after else base, self.cfg.limits.backoff_max_seconds)
        # full jitter, so parallel books don't retry in lockstep
        await asyncio.sleep(delay * (0.5 + random.random() / 2))

    async def _once(self, req: LLMRequest, started: float) -> LLMResponse:
        try:
            from google.genai import errors as genai_errors
        except ImportError as exc:  # pragma: no cover
            raise LLMNotConfigured("google-genai is not installed") from exc
        try:
            interaction: Any = await asyncio.wait_for(
                self._client.aio.interactions.create(
                    model=req.model,
                    input=req.prompt,
                    system_instruction=req.system,
                    generation_config={
                        "temperature": req.temperature,
                        "max_output_tokens": req.max_output_tokens,
                        "thinking_level": req.thinking_level,
                    },
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": req.schema,
                    },
                ),
                timeout=req.timeout_seconds,
            )
        except TimeoutError as exc:
            raise LLMTimeout(f"no response within {req.timeout_seconds:.0f}s") from exc
        except genai_errors.APIError as exc:
            code = getattr(exc, "code", 0) or 0
            if code == 429:
                raise LLMRateLimited(f"provider rate limit (429): {exc.message or exc}") from exc
            if code in RETRYABLE_STATUS:
                raise _Retryable(f"provider error {code}: {exc.message or exc}") from exc
            raise LLMError(f"provider error {code}: {exc.message or exc}") from exc
        except Exception as exc:  # fail safe: any transport failure is still NO TRADE
            raise _Retryable(f"LLM transport error: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        status = getattr(interaction, "status", None)
        if status not in (None, "completed"):
            errs = "; ".join(
                f"{getattr(e, 'code', '')} {getattr(e, 'message', '')}".strip()
                for e in (getattr(interaction, "errors", None) or [])
            )
            raise LLMInvalidOutput(f"interaction status '{status}'{f': {errs}' if errs else ''}")
        text = (getattr(interaction, "output_text", None) or "").strip()
        if not text:
            raise LLMInvalidOutput("empty response from the model")
        usage = getattr(interaction, "usage", None)
        return LLMResponse(
            text=text,
            model=str(getattr(interaction, "model", req.model) or req.model),
            input_tokens=getattr(usage, "total_input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "total_output_tokens", None) if usage else None,
            thought_tokens=getattr(usage, "total_thought_tokens", None) if usage else None,
            total_tokens=getattr(usage, "total_tokens", None) if usage else None,
            latency_ms=latency_ms,
            raw={"id": getattr(interaction, "id", None), "status": status},
        )


class _Retryable(Exception):
    """Internal marker: this failure is worth another attempt."""
