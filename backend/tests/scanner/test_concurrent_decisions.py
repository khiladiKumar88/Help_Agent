"""Two signals on the same candle close must be decided at the same time, not one after the other.

The reported symptom: BTC and ETH both fired on one 15m close, BTC's decision took ~36s, and ETH's
LLM call did not even START until BTC's had come back — so ETH got its first look at the market
nearly a minute after the bar it was judging had closed. The decision was awaited inline on the
candle-publish path, which meant every signal queued behind the one before it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from papermind.core.clock import ReplayClock
from papermind.core.config import Mode, Settings
from papermind.core.events import Topic
from papermind.core.ids import new_id
from papermind.core.types import Decision, Direction, Signal
from papermind.engine import Engine, EngineOptions
from papermind.scanner.agent import AgentExecutor
from tests.conftest import BOOK_ID, BTC, T0, Env, build_env, make_cfg

D = Decimal
ETH = "crypto:binanceusdm:ETH/USDT:USDT"


@dataclass
class SlowDecider:
    """Takes `delay` to answer, and records when each decision started and finished."""

    delay: float = 0.2
    started: list[tuple[str, float]] = field(default_factory=list)
    finished: list[tuple[str, float]] = field(default_factory=list)

    async def decide(self, signal: Signal) -> Decision:
        loop = asyncio.get_running_loop()
        self.started.append((signal.instrument_id, loop.time()))
        await asyncio.sleep(self.delay)
        self.finished.append((signal.instrument_id, loop.time()))
        return Decision(take=False, by="llm", reason="not this one")


def signal(env: Env, instrument_id: str) -> Signal:
    sig = Signal.model_validate(
        {
            "id": new_id("sig"),
            "ts": env.clock.now(),
            "book_id": BOOK_ID,
            "instrument_id": instrument_id,
            "strategy_id": "ema_pullback",
            "timeframe": "15m",
            "direction": Direction.LONG,
            "entry_ref": D("65000"),
            "stop_loss": D("64000"),
            "target": D("67000"),
            "setup": "pullback to EMA21",
            "regime": {"label": "trending_up", "adx": 30.0, "time_bucket": "europe", "expiry_day": False},
            "context": {"close": 65010.0},
        }
    )
    env.journal.save_signal(sig)
    return sig


def executor(env: Env, decider: Any, *, concurrent: bool) -> AgentExecutor:
    return AgentExecutor(
        env.bus,
        env.broker,
        env.journal,
        decider,
        clock=env.clock,
        concurrent_decisions=concurrent,
    )


class TestConcurrentDecisions:
    async def test_two_signals_on_one_candle_are_decided_together(self) -> None:
        env = build_env(make_cfg())
        decider = SlowDecider(delay=0.2)
        ex = executor(env, decider, concurrent=True)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        # exactly what the scanner does: publish one signal, then the next
        await env.bus.publish(Topic.SIGNAL, signal(env, BTC))
        await env.bus.publish(Topic.SIGNAL, signal(env, ETH))
        published = loop.time() - t0
        await ex.drain_decisions()
        total = loop.time() - t0

        assert published < 0.2, "publishing must not wait for the decision"
        assert total < 0.4, f"the two decisions ran back to back ({total:.2f}s), not together"
        assert len(decider.finished) == 2
        # the second decision started before the first one finished: that is the whole point
        assert decider.started[1][1] < decider.finished[0][1]

    async def test_the_second_signal_is_not_delayed_by_the_first(self) -> None:
        env = build_env(make_cfg())
        decider = SlowDecider(delay=0.2)
        ex = executor(env, decider, concurrent=True)
        await env.bus.publish(Topic.SIGNAL, signal(env, BTC))
        await env.bus.publish(Topic.SIGNAL, signal(env, ETH))
        await ex.drain_decisions()
        gap = decider.started[1][1] - decider.started[0][1]
        assert gap < 0.1, f"ETH waited {gap:.2f}s for its first look at the market"

    async def test_both_decisions_are_recorded(self) -> None:
        env = build_env(make_cfg())
        ex = executor(env, SlowDecider(delay=0.01), concurrent=True)
        btc, eth = signal(env, BTC), signal(env, ETH)
        await env.bus.publish(Topic.SIGNAL, btc)
        await env.bus.publish(Topic.SIGNAL, eth)
        await ex.drain_decisions()
        for sig in (btc, eth):
            row = env.journal.get_signal(sig.id)
            assert row is not None and row["decision"] == "skip" and row["decision_by"] == "llm"

    async def test_a_decider_that_raises_is_still_no_trade(self) -> None:
        """A failure inside a decision task must not vanish into the event loop."""

        class Boom:
            async def decide(self, signal: Signal) -> Decision:
                raise RuntimeError("provider exploded")

        env = build_env(make_cfg())
        ex = executor(env, Boom(), concurrent=True)
        sig = signal(env, BTC)
        await env.bus.publish(Topic.SIGNAL, sig)
        await ex.drain_decisions()
        row = env.journal.get_signal(sig.id)
        assert row is not None and row["decision"] == "skip" and row["decision_by"] == "error"

    async def test_drain_is_safe_with_nothing_in_flight(self) -> None:
        env = build_env(make_cfg())
        ex = executor(env, SlowDecider(), concurrent=True)
        await ex.drain_decisions()


class TestReplaysStayInOrder:
    """A backtest has to give the same answer twice, and ordering is part of that."""

    async def test_decisions_are_sequential_when_concurrency_is_off(self) -> None:
        env = build_env(make_cfg())
        decider = SlowDecider(delay=0.05)
        executor(env, decider, concurrent=False)  # subscribes itself to the bus
        await env.bus.publish(Topic.SIGNAL, signal(env, BTC))
        await env.bus.publish(Topic.SIGNAL, signal(env, ETH))
        assert len(decider.finished) == 2, "publishing returns only once the decision is made"
        # the first decision finished before the second one started
        assert decider.finished[0][1] <= decider.started[1][1]

    async def test_a_replay_clock_forces_sequential_decisions(self) -> None:
        """The engine refuses concurrency on a replay clock, whatever the options say."""
        cfg = make_cfg()
        engine = Engine(
            cfg,
            Settings(_env_file=None),
            ReplayClock(T0),
            options=EngineOptions(drive_timers=False, build_llm=False, concurrent_decisions=True),
        )
        assert engine.executor.concurrent_decisions is False


async def test_the_unfiltered_shadow_is_still_traded_before_any_decision() -> None:
    """shadow_raw is the control group: it must never be contingent on the decider."""
    order: list[str] = []

    class Watching(SlowDecider):
        async def decide(self, signal: Signal) -> Decision:
            order.append("decide")
            return await super().decide(signal)

    env = build_env(make_cfg())
    env.broker.set_mode(BOOK_ID, Mode.MANUAL.value)
    ex = executor(env, Watching(delay=0.01), concurrent=True)
    original = ex._shadow_raw

    async def watched(sig: Signal) -> None:
        order.append("shadow_raw")
        await original(sig)

    ex._shadow_raw = watched  # type: ignore[method-assign]
    await env.bus.publish(Topic.SIGNAL, signal(env, BTC))
    await ex.drain_decisions()
    assert order[0] == "shadow_raw"
