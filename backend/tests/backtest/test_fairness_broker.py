"""Broker-level fairness: random entries on a driftless tick walk must have ZERO expectancy.

This drives `PaperBroker.on_tick` directly — no history store, no replay provider, no strategy —
so it isolates the fill and OCO machinery from the data generator. That makes it both fast and
sharp enough to catch a bias of a few hundredths of an R, which an end-to-end run cannot: at a
few hundred trades the sampling noise on expectancy is already +/-0.2R.

The arithmetic being checked, for a stop at -1R and a target at +rr*R on a driftless walk
(gambler's ruin):

    P(target first) = 1 / (1 + rr)
    expectancy      = rr/(1+rr) - rr/(1+rr) = 0

so a fair simulator must report ~0R whatever R:R it is given. The sharpest single number is the
average loser: with no costs a stop-out is EXACTLY -1.000R, and anything worse is the fill model
leaking.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from papermind.core.types import Account, Actor, Direction, OrderRequest
from tests.conftest import BOOK_ID, BTC, book_dict, build_env, make_cfg

D = Decimal

ZERO_CHARGES: dict[str, Any] = {
    "kind": "crypto",
    "source_url": "https://example.test/fairness",
    "as_of": "2026-01-01",
    "maker_pct": "0",
    "taker_pct": "0",
}
REAL_CHARGES = {**ZERO_CHARGES, "maker_pct": "0.02", "taker_pct": "0.05"}
NO_SLIP = {"model": "bps", "bps": "0", "est_spread_bps": "0"}
REAL_SLIP = {"model": "bps", "bps": "2", "est_spread_bps": "2"}

TRADES = 500
STEP_BPS = 4.0


@dataclass(frozen=True)
class Sample:
    n: int
    mean_r: float
    se: float
    win_rate: float
    avg_win: float
    avg_loss: float
    cost_r: float

    def sigmas(self, expected: float) -> float:
        return (self.mean_r - expected) / self.se if self.se else 0.0


def cfg(charges: dict[str, Any], slippage: dict[str, Any]) -> Any:
    """A book with the risk limits lifted, so nothing censors the losing tail."""
    book = book_dict()
    book["slippage"] = slippage
    book["charges"] = "probe"
    book["risk"] = {
        **book["risk"],
        "max_open_positions": 2,
        "max_trades_per_day": 1_000_000,
        "daily_loss_limit_pct": "1000000",
        "flatten_on_daily_loss": False,
        "max_risk_per_trade_pct": "0.5",
    }
    book["baseline"] = {"enabled": False, "seed": 1, "max_delay_bars": 0}
    book["shadow_raw_strategy"] = False
    return make_cfg(book, charges={"probe": charges})


async def walk_trades(
    charges: dict[str, Any], slippage: dict[str, Any], rr: float, seed: int, trades: int = TRADES
) -> Sample:
    """Open a trade, walk a driftless tick path until it closes, repeat."""
    import numpy as np

    env = build_env(cfg(charges, slippage))
    rng = np.random.default_rng(seed)
    sigma = STEP_BPS / 10_000.0
    px = D("65000")
    rs: list[float] = []
    costs: list[float] = []
    await env.tick(px)
    guard = 0
    while len(rs) < trades and guard < trades * 4000:
        entry = env.market.last(BTC).ltp  # type: ignore[union-attr]
        risk = entry * D("0.003")
        req = OrderRequest(
            book_id=BOOK_ID,
            account=Account.MAIN,
            actor=Actor.AGENT,
            instrument_id=BTC,
            direction=Direction.LONG,
            qty=D("0.001"),
            stop_loss=(entry - risk).quantize(D("0.1")),
            target=(entry + risk * D(str(rr))).quantize(D("0.1")),
        )
        _decision, trade = await env.broker.submit(req)
        if trade is None:  # e.g. too many open positions — walk on and retry
            px = px * D(str(math.exp(rng.normal(0, sigma))))
            await env.tick(px.quantize(D("0.1")))
            guard += 1
            continue
        while trade.closed_at is None and guard < trades * 4000:
            px = px * D(str(math.exp(rng.normal(0, sigma))))
            await env.tick(px.quantize(D("0.1")))
            guard += 1
            trade = env.broker._trades.get(trade.id, trade)
        if trade.r_multiple is None or str(trade.exit_reason) not in ("stop_loss", "target"):
            continue
        rs.append(float(trade.r_multiple))
        if trade.initial_risk and trade.initial_risk > 0:
            costs.append(float((trade.charges - trade.funding) / trade.initial_risk))

    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    sd = statistics.stdev(rs) if len(rs) > 1 else 0.0
    return Sample(
        n=len(rs),
        mean_r=statistics.fmean(rs) if rs else 0.0,
        se=sd / math.sqrt(len(rs)) if rs else 0.0,
        win_rate=len(wins) / len(rs) if rs else 0.0,
        avg_win=statistics.fmean(wins) if wins else 0.0,
        avg_loss=statistics.fmean(losses) if losses else 0.0,
        cost_r=statistics.fmean(costs) if costs else 0.0,
    )


@pytest.fixture(scope="module")
def free_rr2(anyio_backend: Any = None) -> Sample:
    import asyncio

    return asyncio.run(walk_trades(ZERO_CHARGES, NO_SLIP, rr=2.0, seed=1))


@pytest.fixture(scope="module")
def free_rr1(anyio_backend: Any = None) -> Sample:
    import asyncio

    return asyncio.run(walk_trades(ZERO_CHARGES, NO_SLIP, rr=1.0, seed=7))


@pytest.fixture(scope="module")
def paid_rr2(anyio_backend: Any = None) -> Sample:
    import asyncio

    return asyncio.run(walk_trades(REAL_CHARGES, REAL_SLIP, rr=2.0, seed=1))


class TestFrictionless:
    def test_the_sample_is_usable(self, free_rr2: Sample) -> None:
        assert free_rr2.n >= TRADES * 0.9

    def test_expectancy_is_zero(self, free_rr2: Sample) -> None:
        assert abs(free_rr2.sigmas(0.0)) <= 4, (
            f"expectancy {free_rr2.mean_r:+.4f}R over {free_rr2.n} random entries on a driftless "
            f"walk ({free_rr2.sigmas(0.0):+.2f} sigma). A fair simulator returns 0R."
        )

    def test_expectancy_is_zero_at_one_to_one_too(self, free_rr1: Sample) -> None:
        """R:R must not change the answer: zero is zero at any ratio."""
        assert abs(free_rr1.sigmas(0.0)) <= 4, f"{free_rr1.mean_r:+.4f}R at rr=1"

    def test_a_stop_out_costs_exactly_one_r(self, free_rr2: Sample) -> None:
        """The sharp test. Overshoot in the stop fill shows up here before anywhere else."""
        assert free_rr2.avg_loss == pytest.approx(-1.0, abs=0.02), (
            f"average loser {free_rr2.avg_loss:+.4f}R; with no costs a stop-out is exactly -1R, "
            "so the difference is the fill model giving R away"
        )

    def test_a_target_pays_the_full_reward(self, free_rr2: Sample) -> None:
        assert free_rr2.avg_win == pytest.approx(2.0, abs=0.05)

    @pytest.mark.parametrize(("rr", "seed"), [(2.0, 1), (1.0, 7)])
    def test_the_win_rate_matches_gamblers_ruin(self, rr: float, seed: int, request: Any) -> None:
        s: Sample = request.getfixturevalue("free_rr2" if rr == 2.0 else "free_rr1")
        theory = 1 / (1 + rr)
        se_p = math.sqrt(theory * (1 - theory) / s.n)
        assert abs(s.win_rate - theory) <= 4 * se_p, (
            f"win rate {s.win_rate:.3f} vs the exact {theory:.3f} at rr={rr} "
            f"({(s.win_rate - theory) / se_p:+.2f} sigma)"
        )

    def test_nothing_was_charged(self, free_rr2: Sample) -> None:
        assert abs(free_rr2.cost_r) < 1e-9


class TestWithCosts:
    def test_expectancy_is_exactly_the_cost_and_no_worse(self, paid_rr2: Sample) -> None:
        expected = -paid_rr2.cost_r
        assert abs(paid_rr2.sigmas(expected)) <= 4, (
            f"expectancy {paid_rr2.mean_r:+.4f}R against a modelled cost of {expected:+.4f}R "
            f"({paid_rr2.sigmas(expected):+.2f} sigma). Anything beyond the cost is a bias."
        )

    def test_costs_were_really_charged(self, paid_rr2: Sample) -> None:
        assert paid_rr2.cost_r > 0.01

    def test_a_loser_is_fully_explained_by_one_r_plus_its_costs(self, paid_rr2: Sample) -> None:
        """Losers may be worse than -1R, but every part of it must be accounted for.

        The budget, all derived rather than guessed:
          -1R                     the stop itself
          - cost_r                the fees actually charged, as measured
          - exit slippage in R    the model's bps against this probe's stop distance
        Anything left over is unmodelled leakage, which is what the stop-fill bug was.
        """
        stop_bps = 0.003 * 10_000  # the probe's stop sits 30 bps away
        exit_slip_r = float(REAL_SLIP["bps"]) / stop_bps
        budget = -(1.0 + paid_rr2.cost_r + exit_slip_r)
        unexplained = budget - paid_rr2.avg_loss  # positive => worse than the budget allows
        assert unexplained <= 0.05, (
            f"average loser {paid_rr2.avg_loss:+.4f}R, but -1R plus {paid_rr2.cost_r:.4f}R of fees "
            f"plus {exit_slip_r:.4f}R of modelled slippage only accounts for {budget:+.4f}R. "
            f"{unexplained:+.4f}R is unexplained."
        )
