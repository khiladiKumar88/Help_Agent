"""The R breakdown: pure functions over trades, fills and candles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from papermind.backtest.diagnostics import exit_reason_buckets, run_diagnostics
from papermind.core.types import Account, Actor, Candle, Direction, ExitReason, TradeStatus
from papermind.journal.entities import Trade

D = Decimal
T0 = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def trade(
    n: int = 0,
    *,
    entry: str = "100",
    exit_: str = "95",
    stop: str = "95",
    target: str | None = "110",
    r: str = "-1.0",
    net: str = "-10",
    charges: str = "0",
    reason: ExitReason = ExitReason.STOP_LOSS,
    direction: Direction = Direction.LONG,
    risk: str = "10",
) -> Trade:
    return Trade(
        id=f"t{n}",
        book_id="b",
        account=Account.MAIN,
        actor=Actor.AGENT,
        instrument_id="i",
        direction=direction,
        status=TradeStatus.CLOSED,
        qty=D("2"),
        contract_size=D("1"),
        leverage=D("1"),
        initial_sl=D(stop),
        current_sl=D(stop),
        created_at=T0,
        target=D(target) if target else None,
        avg_entry=D(entry),
        avg_exit=D(exit_),
        initial_risk=D(risk),
        charges=D(charges),
        net_pnl=D(net),
        r_multiple=D(r),
        exit_reason=reason,
        opened_at=T0 + timedelta(minutes=n),
        closed_at=T0 + timedelta(minutes=n + 30),
    )


def fill(price: str, ref: str, slip: str) -> dict[str, Any]:
    return {"price": D(price), "reference_price": D(ref), "slippage": D(slip)}


def candle(low: str, high: str, minutes: int = 0) -> Candle:
    return Candle(
        instrument_id="i",
        timeframe="15m",
        ts_open=T0 + timedelta(minutes=minutes),
        open=D(low),
        high=D(high),
        low=D(low),
        close=D(high),
        volume=D("1"),
        closed=True,
    )


class TestExitReasons:
    def test_groups_and_averages_by_reason(self) -> None:
        trades = [
            trade(0, r="-1.0", net="-10"),
            trade(1, r="-1.0", net="-10"),
            trade(2, r="2.0", net="20", reason=ExitReason.TARGET),
        ]
        buckets = {b.reason: b for b in exit_reason_buckets(trades)}
        assert buckets["stop_loss"].trades == 2 and buckets["stop_loss"].avg_r == -1.0
        assert buckets["stop_loss"].total_net == D("-20")
        assert buckets["target"].trades == 1 and buckets["target"].avg_r == 2.0
        assert buckets["stop_loss"].share == pytest.approx(2 / 3, abs=1e-3)

    def test_sorted_by_frequency(self) -> None:
        trades = [trade(0), trade(1), trade(2, reason=ExitReason.TARGET)]
        assert next(b.reason for b in exit_reason_buckets(trades)) == "stop_loss"

    def test_a_missing_reason_is_named_rather_than_dropped(self) -> None:
        t = trade(0)
        object.__setattr__(t, "exit_reason", None)
        assert exit_reason_buckets([t])[0].reason == "none"


class TestWinnersAndLosers:
    def test_splits_the_two_sides(self) -> None:
        d = run_diagnostics([trade(0, r="-1.0"), trade(1, r="2.0", reason=ExitReason.TARGET)])
        assert d.avg_loser_r == -1.0 and d.avg_winner_r == 2.0
        assert d.win_rate == 0.5 and d.avg_r == 0.5

    def test_a_loser_worse_than_one_r_is_called_out(self) -> None:
        d = run_diagnostics([trade(i, r="-1.3") for i in range(3)])
        assert d.avg_loser_r == -1.3
        assert any("worse than the -1R" in n for n in d.notes)

    def test_a_clean_one_r_loser_raises_no_note(self) -> None:
        d = run_diagnostics([trade(i, r="-1.0") for i in range(3)])
        assert not any("worse than" in n for n in d.notes)

    def test_open_trades_are_ignored(self) -> None:
        t = trade(0)
        object.__setattr__(t, "closed_at", None)
        assert run_diagnostics([t]).trades == 0


class TestCosts:
    def test_fees_are_expressed_in_r(self) -> None:
        # charges 2 against an initial risk of 10 => 0.2R
        d = run_diagnostics([trade(0, charges="2", risk="10")])
        assert d.fees_in_r == 0.2

    def test_slippage_splits_entry_from_exit(self) -> None:
        """The first fill of a trade is its entry; the rest are exits."""
        t = trade(0, risk="10")
        fills = {t.id: [fill("100.1", "100", "0.1"), fill("94.9", "95", "0.1")]}
        d = run_diagnostics([t], fills)
        assert d.entry_slippage_bps == pytest.approx(10.0, abs=0.1)  # 0.1 on 100
        assert d.exit_slippage_bps == pytest.approx(10.5, abs=0.2)  # 0.1 on 95
        # the trade's 1R is |entry - stop| = 5, so 0.1 of slippage is 0.02R
        assert d.entry_slippage_in_r == pytest.approx(0.02, abs=1e-6)

    def test_no_fills_leaves_slippage_unknown_rather_than_zero(self) -> None:
        d = run_diagnostics([trade(0)])
        assert d.entry_slippage_bps is None and d.exit_slippage_in_r is None


class TestStopOvershoot:
    def test_a_stop_filled_at_its_level_shows_no_overshoot(self) -> None:
        d = run_diagnostics([trade(0, entry="100", stop="95", exit_="95")])
        assert d.stop_overshoot_bps == 0.0 and d.stop_overshoot_in_r == 0.0
        assert d.stop_exits == 1

    def test_a_stop_filled_past_its_level_is_measured(self) -> None:
        """This is the number that exposed the old fill bug."""
        # stop at 95, filled at 94.5 => 0.5 past, on a 1R of 5 => 0.1R
        d = run_diagnostics([trade(0, entry="100", stop="95", exit_="94.5")])
        assert d.stop_overshoot_in_r == pytest.approx(0.1, abs=1e-6)
        assert d.stop_overshoot_bps == pytest.approx(52.6, abs=0.5)  # 0.5 on 95

    def test_overshoot_is_direction_aware(self) -> None:
        # a short's stop is above: stop 105, filled at 106 => 1 past, 1R = 5 => 0.2R
        d = run_diagnostics([trade(0, direction=Direction.SHORT, entry="100", stop="105", exit_="106", target="90")])
        assert d.stop_overshoot_in_r == pytest.approx(0.2, abs=1e-6)

    def test_only_stop_exits_are_counted(self) -> None:
        d = run_diagnostics([trade(0, reason=ExitReason.TARGET, exit_="110", r="2.0")])
        assert d.stop_exits == 0 and d.stop_overshoot_in_r is None


class TestBothLevelsInOneBar:
    def test_detects_a_bar_that_spans_stop_and_target(self) -> None:
        # stop 95, target 110; this bar covers 94..111, so OHLC cannot say which came first
        d = run_diagnostics([trade(0)], None, [candle("94", "111")])
        assert d.both_levels_in_one_bar_pct == 1.0 and d.bars_checked == 1
        assert any("one bar" in n for n in d.notes)

    def test_a_narrow_bar_is_not_counted(self) -> None:
        d = run_diagnostics([trade(0)], None, [candle("94", "100")])
        assert d.both_levels_in_one_bar_pct == 0.0

    def test_bars_outside_the_holding_period_are_ignored(self) -> None:
        d = run_diagnostics([trade(0)], None, [candle("94", "111", minutes=999)])
        assert d.both_levels_in_one_bar_pct == 0.0

    def test_trades_without_a_target_are_not_checked(self) -> None:
        d = run_diagnostics([trade(0, target=None)], None, [candle("94", "111")])
        assert d.bars_checked == 0 and d.both_levels_in_one_bar_pct is None

    def test_no_candles_means_unknown_not_zero(self) -> None:
        assert run_diagnostics([trade(0)]).both_levels_in_one_bar_pct is None


class TestSerialisation:
    def test_as_dict_is_json_safe(self) -> None:
        import json

        d = run_diagnostics([trade(0, charges="1")], {"t0": [fill("100.1", "100", "0.1")]}, [candle("94", "111")])
        blob = json.dumps(d.as_dict())
        assert '"avg_loser_r"' in blob and '"by_exit_reason"' in blob
        assert isinstance(json.loads(blob)["by_exit_reason"], list)

    def test_an_empty_run_does_not_explode(self) -> None:
        d = run_diagnostics([])
        assert d.trades == 0 and d.avg_r is None and d.by_exit_reason == []
