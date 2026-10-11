"""Walk-forward aggregation: independent windows must not be reported as one account.

The bug this pins: each test window runs its own engine from a fresh copy of the capital, but
the report pooled every window's trades and divided by a single capital. Four 1,000-USDT
accounts each losing 250 came out as a 98% drawdown of one account, and a baseline that lost a
bit more read as 122% — a number that cannot exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from papermind.backtest.metrics import Metrics, combine_metrics, trade_metrics
from papermind.core.types import Account, Actor, Direction, ExitReason, TradeStatus
from papermind.journal.entities import Trade

D = Decimal
CAPITAL = D("1000")
T0 = datetime(2026, 1, 5, tzinfo=UTC)


def trade(net: str, r: str, n: int = 0, risk: str = "10") -> Trade:
    """A closed trade with a given net P&L and R-multiple."""
    return Trade(
        id=f"t{n}",
        book_id="b",
        account=Account.MAIN,
        actor=Actor.AGENT,
        instrument_id="i",
        direction=Direction.LONG,
        status=TradeStatus.CLOSED,
        qty=D("1"),
        contract_size=D("1"),
        leverage=D("1"),
        initial_sl=D("90"),
        current_sl=D("90"),
        created_at=T0,
        avg_entry=D("100"),
        avg_exit=D("101"),
        initial_risk=D(risk),
        net_pnl=D(net),
        r_multiple=D(r),
        exit_reason=ExitReason.TARGET,
        opened_at=T0 + timedelta(minutes=n),
        closed_at=T0 + timedelta(minutes=n + 10),
    )


def losing_window(n_trades: int, each: str, start: int) -> Metrics:
    """A window where every trade loses `each`, so its drawdown is the whole run."""
    trades = [trade(each, "-1.0", n=start + i) for i in range(n_trades)]
    return trade_metrics(trades, CAPITAL)


class TestSingleWindowUnchanged:
    def test_one_window_passes_straight_through(self) -> None:
        m = losing_window(5, "-20", 0)
        assert combine_metrics([m], CAPITAL) is m

    def test_no_windows_is_an_empty_row(self) -> None:
        m = combine_metrics([], CAPITAL)
        assert m.trades == 0 and m.net_profit == D("0") and m.windows == 1


class TestDrawdownCannotExceedOneHundredPercent:
    def test_four_losing_windows_report_the_worst_window_not_the_sum(self) -> None:
        # each window loses 250 of its own 1,000 => 25% drawdown per window
        parts = [losing_window(10, "-25", i * 10) for i in range(4)]
        for p in parts:
            assert p.max_drawdown_pct == pytest.approx(25.0, abs=0.1)
        agg = combine_metrics(parts, CAPITAL)
        assert agg.windows == 4
        assert agg.net_profit == D("-1000"), "money still sums across windows"
        assert agg.trades == 40
        assert agg.max_drawdown_pct == pytest.approx(25.0, abs=0.1), "worst window, not the stitch"
        assert agg.max_drawdown == D("250")

    def test_the_old_pooling_was_what_produced_the_impossible_number(self) -> None:
        """Pooling the same trades against one capital is what read as ~100%."""
        parts = [losing_window(10, "-25", i * 10) for i in range(4)]
        pooled = trade_metrics([t for i in range(4) for t in _trades_of(i)], CAPITAL)
        assert pooled.max_drawdown_pct > 95, "this is the bug, kept here as the counter-example"
        assert combine_metrics(parts, CAPITAL).max_drawdown_pct < 30

    def test_a_window_cannot_exceed_one_hundred_percent_on_its_own(self) -> None:
        agg = combine_metrics([losing_window(10, "-25", i * 10) for i in range(8)], CAPITAL)
        assert 0 <= agg.max_drawdown_pct <= 100


def _trades_of(window: int) -> list[Trade]:
    return [trade("-25", "-1.0", n=window * 10 + i) for i in range(10)]


class TestAggregateConventions:
    def test_counts_and_money_are_sums(self) -> None:
        a = trade_metrics([trade("10", "1.0", 0), trade("-5", "-0.5", 1)], CAPITAL)
        b = trade_metrics([trade("20", "2.0", 2)], CAPITAL)
        agg = combine_metrics([a, b], CAPITAL)
        assert agg.trades == 3 and agg.wins == 2 and agg.losses == 1
        assert agg.net_profit == D("25")
        assert agg.gross_profit == D("30") and agg.gross_loss == D("-5")

    def test_avg_r_is_the_per_trade_average_not_the_per_window_average(self) -> None:
        """A window with 9 trades must not count the same as a window with 1."""
        many = trade_metrics([trade("10", "1.0", i) for i in range(9)], CAPITAL)
        one = trade_metrics([trade("-10", "-1.0", 99)], CAPITAL)
        agg = combine_metrics([many, one], CAPITAL)
        assert agg.avg_r == pytest.approx((9 * 1.0 - 1.0) / 10, abs=1e-6)

    def test_win_rate_is_pooled_over_trades(self) -> None:
        a = trade_metrics([trade("10", "1.0", 0), trade("-5", "-0.5", 1)], CAPITAL)
        b = trade_metrics([trade("-5", "-0.5", 2), trade("-5", "-0.5", 3)], CAPITAL)
        assert combine_metrics([a, b], CAPITAL).win_rate == pytest.approx(0.25)

    def test_return_pct_is_the_average_window(self) -> None:
        a = losing_window(4, "-25", 0)  # -100 on 1000 => -10%
        b = losing_window(8, "-25", 10)  # -200 on 1000 => -20%
        agg = combine_metrics([a, b], CAPITAL)
        assert agg.return_pct == pytest.approx(-15.0, abs=0.01)
        assert agg.net_profit == D("-300"), "the money is still the sum"

    def test_profit_factor_is_recomputed_from_pooled_gross(self) -> None:
        a = trade_metrics([trade("30", "3.0", 0)], CAPITAL)
        b = trade_metrics([trade("-10", "-1.0", 1)], CAPITAL)
        assert combine_metrics([a, b], CAPITAL).profit_factor == pytest.approx(3.0)

    def test_best_and_worst_span_every_window(self) -> None:
        a = trade_metrics([trade("30", "3.0", 0)], CAPITAL)
        b = trade_metrics([trade("-40", "-4.0", 1)], CAPITAL)
        agg = combine_metrics([a, b], CAPITAL)
        assert agg.best_trade == D("30") and agg.worst_trade == D("-40")

    def test_windows_is_one_for_a_plain_backtest(self) -> None:
        assert trade_metrics([trade("1", "0.1", 0)], CAPITAL).windows == 1
