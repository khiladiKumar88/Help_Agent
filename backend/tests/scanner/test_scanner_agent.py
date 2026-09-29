"""Scanner -> agent -> risk -> broker, and the random baseline, on the replay clock."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from papermind.core.config import AppConfig
from papermind.core.events import Topic
from papermind.core.types import Account, Actor, Candle, Decision, Direction, Signal
from papermind.data.candles import CandleBuilder
from papermind.data.market import MarketHub
from papermind.scanner.agent import AgentExecutor, BaselineAgent, RuleDecider, size_order
from papermind.scanner.scanner import Scanner, build_strategies
from papermind.strategies.base import SignalIntent, Strategy, StrategyContext
from tests.conftest import BOOK_ID, BTC, T0, Env, book_dict, build_env, make_cfg

D = Decimal


class ScriptedStrategy(Strategy):
    """Emits a long on every bar whose index is in `fire_on` (test double)."""

    id = "scripted"
    name = description = "scripted"
    default_params = {"x": 1}
    param_grid = {"x": [1]}

    def __init__(self, fire_on: set[int] | None = None, boom: bool = False) -> None:
        super().__init__()
        self.fire_on = fire_on or set()
        self.boom = boom
        self.calls = 0

    @property
    def warmup_bars(self) -> int:
        return 5

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        self.calls += 1
        if self.boom:
            raise RuntimeError("strategy bug")
        if (self.calls - 1) in self.fire_on:
            c = D(str(ctx.frame.last(ctx.frame.close)))
            return SignalIntent(Direction.LONG, c, c - 500, c + 1000, "scripted setup", {"k": 1})
        return None


@dataclass
class Rig:
    env: Env
    hub: MarketHub
    scanner: Scanner
    strat: ScriptedStrategy
    signals: list[Signal] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    async def bar(self, i: int, close: str = "65000", tf: str = "15m") -> None:
        """Close one 15m candle (built from a single 15m base candle) + a tick at its close."""
        ts = T0 + timedelta(minutes=15 * i)
        self.env.clock.set(ts + timedelta(minutes=15) - timedelta(milliseconds=1))
        await self.env.tick(close, bid=str(D(close) - D("0.1")), ask=close, advance=0)
        self.env.clock.set(ts + timedelta(minutes=15))
        c = Candle(
            instrument_id=BTC,
            timeframe=tf,
            ts_open=ts,
            open=D(close),
            high=D(close) + 50,
            low=D(close) - 50,
            close=D(close),
            volume=D(1),
        )
        await self.hub.on_base_candle(c)


def make_rig(
    cfg: AppConfig | None = None,
    fire_on: set[int] | None = None,
    boom: bool = False,
    force: bool = True,
    starts: Any = None,
) -> Rig:
    env = build_env(cfg)
    builder = CandleBuilder(timeframes=["1h"], base_tf="15m")
    hub = MarketHub(env.clock, env.bus, env.market, builder)
    strat = ScriptedStrategy(fire_on, boom)
    scanner = Scanner(
        env.cfg,
        env.clock,
        env.bus,
        env.registry,
        builder,
        env.journal,
        strategies={BOOK_ID: [strat]},
        trading_starts_at=starts,
    )
    AgentExecutor(env.bus, env.broker, env.journal, RuleDecider(), force_execute=force)
    BaselineAgent(env.bus, env.broker, env.journal, env.clock)
    rig = Rig(env, hub, scanner, strat)
    env.bus.subscribe(Topic.SIGNAL, rig.signals.append)
    env.bus.subscribe(Topic.AGENT_DECISION, rig.decisions.append)
    return rig


async def test_signal_flows_to_agent_trade_and_is_persisted() -> None:
    rig = make_rig(fire_on={1})  # 2nd evaluation (evaluations start after the 5-bar warm-up)
    for i in range(8):
        await rig.bar(i)
    assert len(rig.signals) == 1 and len(rig.decisions) == 1
    sig = rig.signals[0]
    assert sig.strategy_id == "scripted" and sig.regime["label"] == "unknown"
    assert sig.context["tag_k"] == 1 and "rsi14" in sig.context
    row = rig.env.journal.list_signals(BOOK_ID)[0]
    assert row["decision"] == "take" and row["decision_by"] == "rules" and row["executed"] is True
    agent = [t for t in rig.env.journal.list_trades(BOOK_ID) if t.actor is Actor.AGENT]
    assert len(agent) == 1 and agent[0].signal_id == sig.id and agent[0].qty > 0
    assert agent[0].leverage == D("3")  # agent uses the book's max leverage for margin; risk cap sets size


async def test_manual_mode_records_but_does_not_trade() -> None:
    rig = make_rig(fire_on={1}, force=False)
    for i in range(8):
        await rig.bar(i)
    assert len(rig.signals) == 1
    assert not [t for t in rig.env.journal.list_trades(BOOK_ID) if t.actor is Actor.AGENT]
    assert rig.env.journal.list_signals(BOOK_ID)[0]["executed"] is False


async def test_warmup_cooldown_and_trading_start() -> None:
    rig = make_rig(fire_on={0, 1, 2, 3, 4}, starts=T0 + timedelta(minutes=15 * 7))
    for i in range(12):
        await rig.bar(i)
    # warm-up: evaluated only from bar 4 (5 candles); suppressed until bar 7 closes; cooldown 3 bars
    assert rig.strat.calls > 0
    assert all(s.ts > T0 + timedelta(minutes=15 * 7) for s in rig.signals)
    ts = [s.ts for s in rig.signals]
    assert all(b - a >= timedelta(minutes=45) for a, b in itertools.pairwise(ts))


async def test_failing_strategy_is_isolated() -> None:
    rig = make_rig(boom=True)
    for i in range(8):
        await rig.bar(i)
    assert rig.scanner.errors >= 1 and rig.signals == []


async def test_non_signal_timeframe_and_forming_candles_ignored() -> None:
    rig = make_rig(fire_on=set(range(20)))
    c = Candle(instrument_id=BTC, timeframe="1h", ts_open=T0, open=D(1), high=D(1), low=D(1), close=D(1))
    await rig.scanner.on_candle(c)
    await rig.scanner.on_candle(c.model_copy(update={"closed": False, "timeframe": "15m"}))
    await rig.scanner.on_candle(c.model_copy(update={"instrument_id": "unknown"}))
    assert rig.strat.calls == 0


class SkipDecider:
    async def decide(self, signal: Signal) -> Decision:
        return Decision(take=False, by="rules", reason="nope")


class BrokenDecider:
    async def decide(self, signal: Signal) -> Decision:
        raise TimeoutError("llm down")


@pytest.mark.parametrize("decider", [SkipDecider(), BrokenDecider()])
async def test_skip_or_decider_failure_means_no_trade(decider: Any) -> None:
    env = build_env()
    ex = AgentExecutor(env.bus, env.broker, env.journal, decider, force_execute=True)
    await env.tick("65000", bid="64999.9", ask="65000")
    sig = Signal(
        id="sig_1",
        ts=env.clock.now(),
        book_id=BOOK_ID,
        instrument_id=BTC,
        strategy_id="s",
        timeframe="15m",
        direction=Direction.LONG,
        entry_ref=D("65000"),
        stop_loss=D("64500"),
        target=D("66000"),
    )
    env.journal.save_signal(sig)
    await ex.on_signal(sig)
    row = env.journal.list_signals(BOOK_ID)[0]
    assert row["decision"] == "skip" and not env.broker.active_trades()
    if isinstance(decider, BrokenDecider):
        assert row["decision_by"] == "error" and "llm down" in row["decision_reason"]


async def test_risk_block_is_recorded_on_signal() -> None:
    env = build_env()
    ex = AgentExecutor(env.bus, env.broker, env.journal, RuleDecider(), force_execute=True)
    await env.tick("65000", bid="64999.9", ask="65000")
    env.clock.advance(30)  # data now stale
    sig = Signal(
        id="sig_2",
        ts=env.clock.now(),
        book_id=BOOK_ID,
        instrument_id=BTC,
        strategy_id="s",
        timeframe="15m",
        direction=Direction.LONG,
        entry_ref=D("65000"),
        stop_loss=D("64500"),
    )
    env.journal.save_signal(sig)
    await ex.on_signal(sig)
    row = env.journal.list_signals(BOOK_ID)[0]
    assert row["executed"] is False and row["risk_rule_blocked"] == "R004_STALE_DATA"


async def test_size_order_respects_risk_and_margin() -> None:
    env = build_env()
    await env.tick("65000", bid="64999.9", ask="65000")
    req = env.req(qty="1", sl="64000", leverage=D("3"))
    assert size_order(env.broker, req) == D("0.009")  # 1% risk cap = 10 USDT incl. fees
    tight = env.req(qty="1", sl="64990", leverage=D("1"))
    assert size_order(env.broker, tight) == D("0.015")  # margin-limited: 1000*0.98 / 65000
    none = env.req(qty="1", sl="10000", leverage=D("1"))
    assert size_order(env.broker, none) == 0


async def test_baseline_random_shadow_trades_are_seeded_and_isolated() -> None:
    results = []
    for _ in range(2):
        rig = make_rig(fire_on={6, 10, 14, 18})
        for i in range(30):
            await rig.bar(i, close=str(65000 + (i % 3) * 10))
        base = [t for t in rig.env.journal.list_trades(BOOK_ID) if t.actor is Actor.BASELINE]
        assert base and all(t.account is Account.SHADOW_BASELINE for t in base)
        # baseline has its own wallet; main balance unaffected by baseline fees
        main_fees = sum((t.charges for t in rig.env.journal.list_trades(BOOK_ID, Account.MAIN)), D(0))
        assert rig.env.journal.balance(BOOK_ID, Account.MAIN) == D("1000") - main_fees + sum(
            (t.gross_pnl + t.funding for t in rig.env.journal.list_trades(BOOK_ID, Account.MAIN)), D(0)
        )
        results.append([(t.direction, t.opened_at) for t in sorted(base, key=lambda t: t.created_at)])
        sigs = rig.env.journal.list_signals(BOOK_ID)
        assert any(s["baseline_trade_id"] for s in sigs)
    assert results[0] == results[1]  # same seed -> same random choices


async def test_baseline_daily_loss_halt_does_not_halt_main_book() -> None:
    env = build_env(make_cfg(book_dict(daily_loss_limit_pct=None, daily_loss_limit_abs="12")))
    await env.tick("65000", bid="64999.9", ask="65000")
    from papermind.scanner.agent import place_sized

    _, tid, _ = await place_sized(
        env.broker, BOOK_ID, Account.SHADOW_BASELINE, Actor.BASELINE, BTC, Direction.LONG, D("64700"), None, None, "t"
    )
    assert tid is not None
    await env.tick("65000", bid="64999.9", ask="65000")
    await env.tick("63000", bid="63000", ask="63000.1")  # gap through the stop: baseline loses > 12
    assert env.broker.halted(BOOK_ID, Account.SHADOW_BASELINE)[0]
    assert env.broker.halted(BOOK_ID, Account.MAIN) == (False, None)
    assert env.journal.get_state("shadow_halts")
    env.clock.advance(hours=24)
    assert env.broker.halted(BOOK_ID, Account.SHADOW_BASELINE) == (False, None)


def test_build_strategies_from_book_config() -> None:
    cfg = make_cfg(
        {
            **book_dict(),
            "strategies": [
                {"id": "ema_pullback"},
                {"id": "orb", "enabled": False},
                {"id": "supertrend_flip", "params": {"mult": 2.0}},
            ],
        }
    )
    strats = build_strategies(cfg.book(BOOK_ID))
    assert [s.id for s in strats] == ["ema_pullback", "supertrend_flip"] and strats[1].params["mult"] == 2.0
    india = {**book_dict(), "market": "india", "id": "india-futures-intraday", "strategies": [{"id": "funding_fade"}]}
    with pytest.raises(ValueError, match="does not support"):
        build_strategies(make_cfg(india).book("india-futures-intraday"))
