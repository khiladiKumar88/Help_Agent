"""The LLM analyst as a Decider. Called once per scanner signal — never per tick.

Fail safe, without exception: a timeout, a 429, an exhausted budget, invalid JSON, a schema
violation or any unexpected error all produce **SKIP**, logged with the reason. The analyst can
only make an order stricter; the pure-code risk rules run afterwards either way.

Every call is recorded in `llm_calls` with its prompt version, prompt, response, latency and
tokens — successes and failures alike.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from papermind.analyst.context import EpisodeSource, LessonSource, build_prompt
from papermind.analyst.schema import AnalystVerdict, apply_adjustments, response_schema
from papermind.core.clock import Clock
from papermind.core.config import AppConfig
from papermind.core.ids import new_id
from papermind.core.types import Decision, Signal
from papermind.instruments.registry import InstrumentRegistry
from papermind.journal.service import Journal
from papermind.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse
from papermind.llm.limiter import RateLimiter

log = logging.getLogger(__name__)


class LLMAnalyst:
    """Decider backed by an LLM. Construct via `papermind.analyst.factory.build_analyst`."""

    by = "llm"

    def __init__(
        self,
        cfg: AppConfig,
        clock: Clock,
        journal: Journal,
        registry: InstrumentRegistry,
        provider: LLMProvider,
        limiter: RateLimiter,
        lessons: LessonSource | None = None,
        episodes: EpisodeSource | None = None,
    ) -> None:
        self.cfg = cfg
        self.llm = cfg.llm
        self.clock = clock
        self.journal = journal
        self.registry = registry
        self.provider = provider
        self.limiter = limiter
        self.lessons = lessons
        self.episodes = episodes
        self.last_pause_reason: str | None = None

    # ------------------------------------------------------------------ status
    @property
    def paused(self) -> bool:
        """True when the daily budget is gone: the agent stops deciding rather than trade blind."""
        return self.limiter.budget_exhausted()

    def status(self) -> dict[str, Any]:
        snap = self.limiter.snapshot()
        return {
            "provider": self.provider.name,
            "model": self.llm.model,
            "prompt_version": self.llm.prompt_version,
            "confidence_threshold": self.llm.confidence_threshold,
            "paused": self.paused,
            "pause_reason": self.last_pause_reason if self.paused else None,
            **snap,
        }

    # ------------------------------------------------------------------ decide
    async def decide(self, signal: Signal) -> Decision:
        book = self.cfg.book(signal.book_id)
        symbol = (
            self.registry.get(signal.instrument_id).symbol
            if signal.instrument_id in self.registry
            else signal.instrument_id
        )
        schema = response_schema()
        system, prompt = build_prompt(
            signal,
            version=self.llm.prompt_version,
            currency=book.currency,
            symbol=symbol,
            schema=schema,
            lessons=self.lessons,
            episodes=self.episodes,
        )
        req = LLMRequest(
            system=system,
            prompt=prompt,
            schema=schema,
            model=self.llm.model,
            temperature=self.llm.temperature,
            max_output_tokens=self.llm.max_output_tokens,
            thinking_level=self.llm.thinking_level,
            timeout_seconds=self.llm.timeout_seconds,
        )
        call_id = new_id("llm")
        prompt_hash = req.cache_key()

        try:
            resp = await self._call(req, prompt_hash)
        except LLMError as exc:
            return self._fail(signal, call_id, req, prompt_hash, exc.reason, str(exc))
        except Exception as exc:  # fail safe: nothing escapes as a trade
            log.exception("analyst call failed")
            return self._fail(signal, call_id, req, prompt_hash, "llm_error", str(exc))

        try:
            verdict = self._parse(resp.text)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            return self._fail(
                signal, call_id, req, prompt_hash, "llm_invalid_output", f"invalid JSON/schema: {exc}", resp=resp
            )

        self._log_call(signal, call_id, req, prompt_hash, resp, ok=True)
        return self._verdict_to_decision(signal, verdict, call_id, resp)

    # ------------------------------------------------------------------ internals
    async def _call(self, req: LLMRequest, prompt_hash: str) -> LLMResponse:
        """Cache first (replays must not burn quota), then the rate limiter, then the provider."""
        if self.llm.cache_responses:
            cached = self.journal.get_llm_cache(prompt_hash, req.model)
            if cached is not None:
                self.limiter.record_cache_hit()
                return LLMResponse(text=cached, model=req.model, cached=True)
        # the budget is enforced in replays too
        await self.limiter.acquire(est_input_tokens=len(req.prompt) // 4)
        try:
            resp = await self.provider.complete(req)
        except LLMError:
            self.limiter.record_result(None, None, error=True)
            raise
        self.limiter.record_result(resp.input_tokens, resp.output_tokens)
        if self.llm.cache_responses:
            self.journal.put_llm_cache(prompt_hash, req.model, resp.text)
        return resp

    def _parse(self, text: str) -> AnalystVerdict:
        raw = text.strip()
        if raw.startswith("```"):  # tolerate a fenced block
            raw = raw.split("```", 2)[1]
            if raw.lstrip().lower().startswith("json"):
                raw = raw.lstrip()[4:]
            raw = raw.strip()
        return AnalystVerdict.model_validate_json(raw)

    def _verdict_to_decision(
        self, signal: Signal, verdict: AnalystVerdict, call_id: str, resp: LLMResponse
    ) -> Decision:
        threshold = self.llm.confidence_threshold
        below = verdict.confidence < threshold
        take = verdict.take and not below
        stop, applied = apply_adjustments(
            signal.direction, signal.entry_ref, signal.stop_loss, verdict.suggested_adjustments
        )
        reason = verdict.reasoning.strip() or ("analyst said TAKE" if verdict.take else "analyst said SKIP")
        if below:
            reason = f"confidence {verdict.confidence} is below the {threshold} threshold — skipped. {reason}".strip()
        self.journal.update_signal(
            signal.id,
            confidence=verdict.confidence,
            reasoning=reason,
            risks_noted=verdict.risks_noted,
            lessons_applied=verdict.lessons_applied,
            adjustments=applied,
            prompt_version=self.llm.prompt_version,
            llm_call_id=call_id,
        )
        return Decision(
            take=take,
            by=self.by,
            reason=reason,
            confidence=verdict.confidence,
            stop_loss=stop if stop != signal.stop_loss else None,
            size_factor=applied.get("size_factor"),
            cached=resp.cached,
        )

    def _fail(
        self,
        signal: Signal,
        call_id: str,
        req: LLMRequest,
        prompt_hash: str,
        reason: str,
        message: str,
        resp: LLMResponse | None = None,
    ) -> Decision:
        """Any failure is NO TRADE, recorded against the signal and in llm_calls."""
        if reason == "llm_budget_exhausted":
            self.last_pause_reason = message
        log.warning("analyst: no trade", extra={"llm_reason": reason, "detail": message, "signal_id": signal.id})
        self._log_call(signal, call_id, req, prompt_hash, resp, ok=False, reason=reason, error=message)
        text = f"NO TRADE — {reason}: {message}"
        self.journal.update_signal(
            signal.id, reasoning=text, prompt_version=self.llm.prompt_version, llm_call_id=call_id
        )
        self.journal.audit("LLM", text, book_id=signal.book_id, level="WARNING", payload={"signal_id": signal.id})
        return Decision(take=False, by=self.by, reason=text)

    def _log_call(
        self,
        signal: Signal,
        call_id: str,
        req: LLMRequest,
        prompt_hash: str,
        resp: LLMResponse | None,
        ok: bool,
        reason: str | None = None,
        error: str | None = None,
    ) -> None:
        self.journal.record_llm_call(
            call_id=call_id,
            book_id=signal.book_id,
            signal_id=signal.id,
            purpose="analyst",
            provider=self.provider.name,
            model=resp.model if resp else req.model,
            prompt_version=self.llm.prompt_version,
            prompt_hash=prompt_hash,
            system_prompt=req.system if self.llm.log_prompts else "",
            prompt=req.prompt if self.llm.log_prompts else "",
            response=(resp.text if resp else ""),
            ok=ok,
            error_reason=reason,
            error=error,
            latency_ms=resp.latency_ms if resp else 0,
            input_tokens=resp.input_tokens if resp else None,
            output_tokens=resp.output_tokens if resp else None,
            thought_tokens=resp.thought_tokens if resp else None,
            total_tokens=resp.total_tokens if resp else None,
            cached=bool(resp and resp.cached),
        )
