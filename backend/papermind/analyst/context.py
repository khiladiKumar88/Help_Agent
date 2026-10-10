"""Builds the compact prompt for one signal, and loads the versioned prompt template.

Compact on purpose: the analyst gets the setup, a short numeric summary of the closed candles
and indicators, the regime and the time bucket. No raw candle dump — it would burn the free-tier
token budget for no gain and bury the parts that matter.

`lessons` and `episodes` are the Phase-4 hooks. They are called here and return empty lists for
now, so wiring the second brain in later is a one-line change rather than a prompt rewrite.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from papermind.core.types import Signal

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Indicator keys worth showing, in a human order, with how to label them.
_INDICATORS: tuple[tuple[str, str], ...] = (
    ("close", "close"),
    ("ema9", "EMA9"),
    ("ema21", "EMA21"),
    ("ema50", "EMA50"),
    ("vwap", "VWAP"),
    ("rsi14", "RSI14"),
    ("atr14", "ATR14"),
    ("adx14", "ADX14"),
    ("plus_di", "+DI"),
    ("minus_di", "-DI"),
    ("funding_rate", "funding rate"),
)


class LessonSource(Protocol):
    """Phase 4 playbook. Returns short lesson lines relevant to this signal."""

    def lessons_for(self, signal: Signal) -> Sequence[str]: ...


class EpisodeSource(Protocol):
    """Phase 4 episodic memory. Returns short descriptions of the most similar past trades."""

    def similar_trades(self, signal: Signal, k: int = 5) -> Sequence[str]: ...


class NoLessons:
    """Placeholder until the playbook lands (Phase 4)."""

    def lessons_for(self, signal: Signal) -> Sequence[str]:
        return ()


class NoEpisodes:
    """Placeholder until episodic memory lands (Phase 4)."""

    def similar_trades(self, signal: Signal, k: int = 5) -> Sequence[str]:
        return ()


@dataclass(frozen=True)
class PromptTemplate:
    version: str
    system: str
    user: str

    def render(self, **sections: str) -> str:
        out = self.user
        for key, value in sections.items():
            out = out.replace("{" + key + "}", value)
        return out


@lru_cache(maxsize=8)
def load_template(version: str) -> PromptTemplate:
    """Read `prompts/<version>.md`, split on the `# SYSTEM` / `# USER` headings."""
    path = PROMPTS_DIR / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt template '{version}' not found in {PROMPTS_DIR}")
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):  # strip the YAML front matter
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    if "# SYSTEM" not in text or "# USER" not in text:
        raise ValueError(f"prompt template '{version}' needs a '# SYSTEM' and a '# USER' section")
    _, rest = text.split("# SYSTEM", 1)
    system, user = rest.split("# USER", 1)
    return PromptTemplate(version=version, system=system.strip(), user=user.strip())


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.6g}"
    return str(value)


def signal_section(signal: Signal, currency: str, symbol: str) -> str:
    risk = abs(signal.entry_ref - signal.stop_loss)
    rr = (abs(signal.target - signal.entry_ref) / risk) if signal.target is not None and risk > 0 else None
    lines = [
        f"- instrument: {symbol} ({signal.instrument_id})",
        f"- strategy: {signal.strategy_id} on {signal.timeframe} candles",
        f"- direction: {signal.direction}",
        f"- setup: {signal.setup or 'n/a'}",
        f"- reference entry: {signal.entry_ref} {currency}",
        f"- stop-loss: {signal.stop_loss} (risk per unit {risk})",
        f"- target: {signal.target if signal.target is not None else 'none'}"
        + (f" (reward:risk {rr:.2f}:1)" if rr is not None else ""),
        f"- signal time: {signal.ts.isoformat()}",
    ]
    if signal.params:
        lines.append(f"- strategy parameters: {', '.join(f'{k}={v}' for k, v in sorted(signal.params.items()))}")
    tags = {k[4:]: v for k, v in signal.context.items() if k.startswith("tag_")}
    if tags:
        lines.append(f"- strategy notes: {', '.join(f'{k}={_fmt(v)}' for k, v in sorted(tags.items()))}")
    return "\n".join(lines)


def market_section(signal: Signal) -> str:
    ctx = signal.context
    rows = [f"- {label}: {_fmt(ctx.get(key))}" for key, label in _INDICATORS if key in ctx]
    if not rows:
        return "No indicator snapshot was available for this bar."
    entry = signal.entry_ref
    close = ctx.get("close")
    if isinstance(close, int | float) and entry:
        rows.append(f"- reference entry vs last close: {float(entry) - float(close):+,.6g}")
    return "\n".join(rows)


def regime_section(signal: Signal) -> str:
    r = signal.regime
    if not r:
        return "Regime was not available."
    keys = (
        ("label", "regime"),
        ("adx", "ADX"),
        ("ema_slope_pct", "EMA50 slope %"),
        ("atr_pct", "ATR % of price"),
        ("atr_pct_rank", "ATR percentile rank"),
        ("atr_expansion", "ATR expansion vs median"),
        ("realized_vol", "realized volatility (annualized)"),
        ("vix", "India VIX"),
        ("time_bucket", "time of day"),
        ("expiry_day", "expiry day"),
    )
    return "\n".join(f"- {label}: {_fmt(r.get(key))}" for key, label in keys if key in r)


def _bullets(items: Sequence[str], empty: str) -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def build_prompt(
    signal: Signal,
    *,
    version: str,
    currency: str,
    symbol: str,
    schema: dict[str, Any],
    lessons: LessonSource | None = None,
    episodes: EpisodeSource | None = None,
) -> tuple[str, str]:
    """Return (system prompt, user prompt) for this signal."""
    tpl = load_template(version)
    lesson_lines = list((lessons or NoLessons()).lessons_for(signal))
    episode_lines = list((episodes or NoEpisodes()).similar_trades(signal))
    user = tpl.render(
        signal=signal_section(signal, currency, symbol),
        market=market_section(signal),
        regime=regime_section(signal),
        lessons=_bullets(lesson_lines, "None yet — the playbook starts empty (it is built in a later phase)."),
        episodes=_bullets(
            episode_lines, "None yet — similar-trade retrieval starts empty (it is built in a later phase)."
        ),
        schema=json.dumps(schema, indent=2, sort_keys=True),
    )
    return tpl.system, user
