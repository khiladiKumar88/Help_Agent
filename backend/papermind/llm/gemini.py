"""Gemini provider via the official `google-genai` SDK (Interactions API).

Public, documented surface only — verified against the installed SDK (2.26):
  client.aio.interactions.create(model=, input=, system_instruction=, generation_config=,
                                 response_format=, timeout=) -> Interaction
  Interaction: .output_text, .status, .errors, .usage.total_{input,output,thought}_tokens

The model id and every limit come from `llm` config — nothing is hardcoded here. The API key is
read from the environment (populated from `.env`) and never appears in a prompt or a log.
Retries use exponential backoff on 5xx; a 429 carries the provider's own retry-after, a 4xx is
fatal and never retried, and the analyst turns any failure into NO TRADE.

Two things about this SDK that the defaults get wrong for a trading loop, both of which turned
provider errors into mystery 60s stalls:

  * the shared httpx client is built with `timeout=None` at EVERY layer — connect, read, write
    AND pool — so a stalled socket or a saturated pool waits forever and only our own
    `asyncio.wait_for` ever ends it. We pass explicit timeouts.
  * `interactions.create` applies its OWN retry policy (4 retries, on 408/409/429/5xx) INSIDE
    one call, and it uses a 429's `Retry-After` verbatim with no ceiling. The free tier answers
    an exhausted daily quota with `retry-after: 45067`, so ONE permitted retry sleeps for 12.5
    hours and the only thing that ends the call is our own `asyncio.wait_for`. That is how a 429
    Google answers in 0.9s reached the call log as `llm_timeout`, every time, for a whole day —
    `llm_rate_limited` never appeared once in 106 failures. The SDK cannot be told to retry
    nothing, so we take 429 off the list of statuses it may retry and keep the retrying here,
    where there is a backoff ceiling.
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

RETRYABLE_STATUS = frozenset({408, 409, 500, 502, 503, 504})  # 429 is handled on its own
# What the SDK's own single retry may act on. 429 is deliberately absent (see the client setup in
# `__init__`); these cannot carry an uncapped Retry-After from this provider, and wait_for bounds
# them anyway.
SDK_RETRY_STATUS = (500, 502, 503, 504)

# Connect/write/pool are short on purpose: they cover getting the request out, not the model
# thinking. Only `read` has to cover the model's own latency, so it tracks the configured budget.
CONNECT_TIMEOUT = 10.0
WRITE_TIMEOUT = 20.0
POOL_TIMEOUT = 10.0
MAX_CONNECTIONS = 8


def _http_status(exc: BaseException) -> int | None:
    """HTTP status behind an SDK exception, whichever error class it happens to be.

    Deliberately duck-typed: the Interactions API raises its own `APIError` hierarchy, which is
    NOT `google.genai.errors.APIError`, so matching on class let every 429 and every 400 fall
    through to the generic transport branch and get retried as if it were a dropped connection.
    """
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and value:
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and status else None


def _retry_after(exc: BaseException) -> float | None:
    """The provider's own Retry-After, in seconds, when it sent one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = float(headers.get("retry-after") or "")
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _message(exc: BaseException) -> str:
    return str(getattr(exc, "message", None) or exc)


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
            import httpx
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMNotConfigured("google-genai is not installed") from exc
        read = max(5.0, cfg.timeout_seconds)
        self._client = genai.Client(
            api_key=key,
            http_options=genai_types.HttpOptions(
                timeout=int(read * 1000),
                retry_options=genai_types.HttpRetryOptions(
                    # Its minimum is one retry: `attempts=0` is silently rewritten to 1 inside
                    # the SDK, and `create` takes no per-call retry argument. So instead we take
                    # 429 off its retryable list, which IS honoured (`status_codes_override`).
                    # That matters because the SDK sleeps for a 429's Retry-After verbatim, with
                    # no ceiling, and this provider answers an exhausted daily quota with
                    # `retry-after: 45067` — one permitted retry would park the call for 12.5
                    # HOURS, and the only thing ending it would be our own wait_for.
                    attempts=1,
                    http_status_codes=list(SDK_RETRY_STATUS),
                ),
                async_client_args={
                    "timeout": httpx.Timeout(
                        connect=CONNECT_TIMEOUT, read=read, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT
                    ),
                    "limits": httpx.Limits(max_connections=MAX_CONNECTIONS, max_keepalive_connections=2),
                },
            ),
        )

    async def complete(self, req: LLMRequest) -> LLMResponse:
        attempts = max(1, self.cfg.limits.max_retries + 1)
        last: Exception | None = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                return await self._once(req, started)
            except LLMRateLimited as exc:
                last = exc
                # A cap we cannot outwait inside this call is not worth retrying: the free tier
                # answers an exhausted daily quota with a retry-after measured in HOURS, and
                # sleeping on it four times just burns the signal's decision window.
                if attempt == attempts - 1 or (
                    exc.retry_after is not None and exc.retry_after > self.cfg.limits.backoff_max_seconds
                ):
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
        except (LLMError, asyncio.CancelledError):
            raise
        except Exception as exc:  # fail safe: any provider failure is still NO TRADE
            status = _http_status(exc)
            if status == 429:
                raise LLMRateLimited(
                    f"provider rate limit (429): {_message(exc)}", retry_after=_retry_after(exc)
                ) from exc
            if status in RETRYABLE_STATUS:
                raise _Retryable(f"provider error {status}: {_message(exc)}") from exc
            if status is not None and 400 <= status < 500:
                # a bad request, a bad key or a bad model id cannot succeed on a second try,
                # and retrying it four times only hides the real message
                raise LLMError(f"provider error {status}: {_message(exc)}") from exc
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
