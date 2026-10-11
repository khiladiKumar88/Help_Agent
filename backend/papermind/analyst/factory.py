"""Builds the analyst from config. One place decides which provider is in play.

`llm.provider`:
  * `gemini` — the real thing. If the API key is missing we do NOT silently fall back to taking
    every signal; the analyst is disabled and the agent stays out of Auto (status says why).
  * `fake`   — deterministic offline provider (tests, fast replays).
  * `off`    — no LLM at all: the rules decider takes every signal. Useful to measure the raw
    strategy, and the only mode that trades without an LLM opinion — so it is explicit, never a
    fallback from a failure.
"""

from __future__ import annotations

import logging
import os

from papermind.analyst.analyst import LLMAnalyst
from papermind.analyst.context import EpisodeSource, LessonSource
from papermind.core.clock import Clock, RealClock
from papermind.core.config import AppConfig, Settings
from papermind.instruments.registry import InstrumentRegistry
from papermind.journal.service import Journal
from papermind.llm.base import LLMNotConfigured, LLMProvider
from papermind.llm.fake import FakeProvider
from papermind.llm.limiter import RateLimiter

log = logging.getLogger(__name__)


class AnalystUnavailable(Exception):
    """The configured analyst could not be built. Carries a human-readable reason for the UI."""


def resolve_api_key(cfg: AppConfig, settings: Settings | None) -> str | None:
    """The provider's API key, from `.env` first and then a plain environment variable.

    `.env` is read by pydantic-settings into `Settings` and is NOT exported into os.environ, so
    looking only at the environment misses a key put exactly where the README says to put it.
    The environment fallback is what makes a custom `llm.api_key_env` name work.
    """
    if settings is not None:
        # the standard field name mirrors the standard env var: GEMINI_API_KEY -> gemini_api_key
        from_env_file = settings.secret(cfg.llm.api_key_env.lower())
        if from_env_file:
            return from_env_file
    value = os.environ.get(cfg.llm.api_key_env, "").strip()
    return value or None


def build_provider(cfg: AppConfig, settings: Settings | None = None) -> LLMProvider | None:
    """Return the configured provider, or None when `llm.provider` is `off`."""
    kind = cfg.llm.provider
    if kind == "off":
        return None
    if kind == "fake":
        return FakeProvider()
    from papermind.llm.gemini import GeminiProvider  # imported lazily: only needed for real runs

    return GeminiProvider(cfg.llm, api_key=resolve_api_key(cfg, settings))


def build_analyst(
    cfg: AppConfig,
    clock: Clock,
    journal: Journal,
    registry: InstrumentRegistry,
    provider: LLMProvider | None = None,
    lessons: LessonSource | None = None,
    episodes: EpisodeSource | None = None,
    settings: Settings | None = None,
) -> LLMAnalyst | None:
    """Build the analyst, or return None when no LLM is configured.

    Raises AnalystUnavailable when an LLM *was* requested but cannot be built (e.g. no API key),
    so the caller can surface the reason instead of quietly trading without an analyst.
    """
    if provider is None:
        if cfg.llm.provider == "off":
            return None
        try:
            provider = build_provider(cfg, settings)
        except LLMNotConfigured as exc:
            raise AnalystUnavailable(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - unexpected SDK/import failure
            raise AnalystUnavailable(f"could not start the {cfg.llm.provider} provider: {exc}") from exc
    if provider is None:
        return None
    # The daily budget protects a REAL resource (your API quota), so its day key must come from
    # real time. Keying it to the replay clock would reset the budget on every simulated day —
    # a 200-day backtest would happily fire 200x the daily limit of genuine API calls.
    limiter = RateLimiter(cfg.llm.limits, RealClock() if clock.is_replay else clock, store=journal)
    return LLMAnalyst(cfg, clock, journal, registry, provider, limiter, lessons=lessons, episodes=episodes)
