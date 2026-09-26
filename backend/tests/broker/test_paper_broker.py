"""PaperBroker end-to-end scenarios on the replay clock. Numbers are worked out by hand.

Defaults (conftest): 1000 USDT book, 1% risk/trade, taker 0.05%, maker 0.02%, slippage 2 bps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from papermind.broker.paper_broker import BrokerError, PaperBroker
from papermind.core.clock import ReplayClock
from papermind.core.types import (
    Account,
    Actor,
    Direction,
    ExitReason,
    FundingEvent,
    LedgerKind,
    OrderPurpose,
    OrderStatus,
    OrderType,
    TradeStatus,
    TrailingConfig,
)
from tests.conftest import BOOK_ID, BTC, ETH, Env, book_dict, build_env, make_cfg

D = Decimal
QUOTE = {"bid": "64999.9", "ask": "65000"}


async def open_long(env: Env, **kw: object) -> str:
    await env.tick("65000", **QUOTE)
    decision, trade = await env.broker.submit(env.req(**kw))  # type: ignore[arg-type]
    assert decision.approved, decision.message
    assert trade is not None
    await env.tick("65000", **QUOTE)
    return trade.id


def pending_orders(env: Env, trade_id: str) -> dict[OrderPurpose, object]:
    return {o.purpose: o for o in env.broker._orders_for(trade_id)}


# ------------------------------------------------------------------ entries


async def test_market_entry_fills_on_next_tick_with_slippage_and_fees(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    decision, trade = await env.broker.submit(env.req(target="66000"))
    assert decision.approved and trade is not None
    assert trade.status is TradeStatus.PENDING
    # a tick at the same timestamp must NOT fill (strictly the next tick)
    env.clock.advance(0)
    assert env.broker._trades[trade.id].status is TradeStatus.PENDING

    await env.tick("65000", **QUOTE)
    t = env.broker._trades[trade.id]
    assert t.status is TradeStatus.OPEN
    assert t.avg_entry == D("65013")  # ask 65000 + 2 bps
    assert t.charges == D("0.16253250")  # 0.005 * 65013 * 0.05%
    assert t.initial_risk == D("5.065")  # (65013 - 64000) * 0.005
    assert env.journal.balance(BOOK_ID, Account.MAIN) == D("1000") - D("0.16253250")
    orders = pending_orders(env, t.id)
    assert set(orders) == {OrderPurpose.STOP, OrderPurpose.TARGET}


async def test_stop_loss_exit_net_pnl_and_r_multiple(env: Env) -> None:
    tid = await open_long(env, target="66000")
    await env.tick("64000", bid="63999.9", ask="64000")
    t = env.journal.get_trade(tid)
    assert t is not None and t.status is TradeStatus.CLOSED
    assert t.exit_reason is ExitReason.STOP_LOSS
    assert t.avg_exit == D("63987.1")  # bid 63999.9 - 2 bps, floored to tick
    assert t.gross_pnl == D("-5.12950000")
    assert t.charges == D("0.32250025")  # 0.1625325 + 0.005*63987.1*0.05%
    assert t.net_pnl == D("-5.45200025")
    assert t.r_multiple == D("-1.08")
    assert env.journal.balance(BOOK_ID, Account.MAIN) == D("1000") + t.net_pnl
    assert not env.broker._orders_for(tid)  # target cancelled (OCO)


async def test_target_exit_is_maker_at_target(env: Env) -> None:
    tid = await open_long(env, target="66000")
    await env.tick("66000", bid="66000", ask="66000.1")
    t = env.journal.get_trade(tid)
    assert t is not None and t.exit_reason is ExitReason.TARGET
    assert t.avg_exit == D("66000")
    assert t.gross_pnl == D("4.93500000")
    assert t.charges == D("0.16253250") + D("0.06600000")  # maker 0.02%
    assert t.net_pnl == D("4.70646750")
    assert t.r_multiple == D("0.93")


async def test_gap_through_stop_fills_at_worse_price(env: Env) -> None:
    tid = await open_long(env)
    await env.tick("63000", bid="63000", ask="63000.1")
    t = env.journal.get_trade(tid)
    assert t is not None and t.avg_exit == D("62987.4")  # 63000 - 2bps: slippage beyond the stop
    assert t.r_multiple is not None and t.r_multiple < D("-2")


async def test_short_trade_round_trip(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    d, trade = await env.broker.submit(env.req(direction=Direction.SHORT, sl="66000", target="64000"))
    assert d.approved and trade
    await env.tick("65000", **QUOTE)
    t = env.broker._trades[trade.id]
    assert t.avg_entry == D("64986.9")  # sell at bid 64999.9 - 2bps floored
    await env.tick("66000", bid="65999.9", ask="66000")  # stop triggers on ask >= 66000
    closed = env.journal.get_trade(trade.id)
    assert closed is not None and closed.exit_reason is ExitReason.STOP_LOSS
    assert closed.avg_exit == D("66013.2")  # 66000 * 1.0002 rounded up
    assert closed.gross_pnl < 0


async def test_orders_only_fill_on_their_own_instrument(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    await env.tick("3000", bid="2999.9", ask="3000", inst=ETH)
    _, trade = await env.broker.submit(env.req())
    assert trade
    await env.tick("3000", bid="2999.9", ask="3000", inst=ETH)
    assert env.broker._trades[trade.id].status is TradeStatus.PENDING


# ------------------------------------------------------------------ limit entries


async def test_resting_limit_entry_fills_as_maker_at_limit(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    _, trade = await env.broker.submit(env.req(order_type=OrderType.LIMIT, limit_price=D("64500"), sl="63800"))
    assert trade
    await env.tick("64800", bid="64799.9", ask="64800")  # not marketable -> resting
    assert env.broker._trades[trade.id].status is TradeStatus.PENDING
    await env.tick("64400", bid="64399.9", ask="64400")
    t = env.broker._trades[trade.id]
    assert t.status is TradeStatus.OPEN and t.avg_entry == D("64500")
    assert t.charges == D("0.06450000")  # maker 0.02% of 322.5


async def test_marketable_limit_is_taker_capped_at_limit(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    _, trade = await env.broker.submit(env.req(order_type=OrderType.LIMIT, limit_price=D("65005"), sl="64000"))
    assert trade
    await env.tick("65000", **QUOTE)
    t = env.broker._trades[trade.id]
    assert t.avg_entry == D("65005")  # would be 65013 with slippage; never worse than the limit
    assert t.charges == D("0.16251250")  # taker


async def test_cancel_pending_entry(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    _, trade = await env.broker.submit(env.req(order_type=OrderType.LIMIT, limit_price=D("64000"), sl="63000"))
    assert trade
    t = await env.broker.cancel(trade.id)
    assert t.status is TradeStatus.CANCELLED
    assert not env.broker.active_trades()
    with pytest.raises(BrokerError):
        await env.broker.cancel(trade.id)


# ------------------------------------------------------------------ manual exit / modify


async def test_manual_close_fills_next_tick(env: Env) -> None:
    tid = await open_long(env)
    await env.broker.close(tid)
    assert env.broker._trades[tid].status is TradeStatus.OPEN  # waits for next tick
    purposes = {o.purpose for o in env.broker._orders_for(tid)}
    assert purposes == {OrderPurpose.EXIT}
    await env.broker.close(tid)  # idempotent while exiting
    await env.tick("65100", bid="65100", ask="65100.1")
    t = env.journal.get_trade(tid)
    assert t is not None and t.exit_reason is ExitReason.MANUAL and t.avg_exit == D("65086.9")
    with pytest.raises(BrokerError):
        await env.broker.close(tid)


async def test_close_pending_trade_cancels_it(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    _, trade = await env.broker.submit(env.req())
    assert trade
    t = await env.broker.close(trade.id)
    assert t.status is TradeStatus.CANCELLED


async def test_modify_stop_and_target(env: Env) -> None:
    tid = await open_long(env, target="66000")
    t = await env.broker.modify(tid, stop_loss=D("64500"))  # tighten
    assert t.current_sl == D("64500")
    stop = pending_orders(env, tid)[OrderPurpose.STOP]
    assert stop.trigger_price == D("64500")  # type: ignore[attr-defined]
    t = await env.broker.modify(tid, target=D("67000"))
    assert pending_orders(env, tid)[OrderPurpose.TARGET].limit_price == D("67000")  # type: ignore[attr-defined]
    t = await env.broker.modify(tid, clear_target=True)
    assert t.target is None and OrderPurpose.TARGET not in pending_orders(env, tid)
    await env.tick("64500", bid="64500", ask="64500.1")
    closed = env.journal.get_trade(tid)
    assert closed is not None and closed.exit_reason is ExitReason.STOP_LOSS


@pytest.mark.parametrize(
    ("kw", "rule"),
    [
        ({"stop_loss": D("65100")}, "R005_STOP_LOSS_VALID"),  # wrong side of price
        ({"stop_loss": D("64000.05")}, "R005_STOP_LOSS_VALID"),  # tick size
        ({"stop_loss": D("62000")}, "R006_MAX_RISK_PER_TRADE"),  # loosening beyond 10 USDT
        ({"target": D("64000")}, "R005_STOP_LOSS_VALID"),
    ],
)
async def test_modify_rejections(env: Env, kw: dict[str, Decimal], rule: str) -> None:
    tid = await open_long(env)
    with pytest.raises(BrokerError) as exc:
        await env.broker.modify(tid, **kw)
    assert exc.value.rule_id == rule


async def test_modify_loosen_within_cap_allowed(env: Env) -> None:
    tid = await open_long(env)
    t = await env.broker.modify(tid, stop_loss=D("63500"))  # risk (65013-63500)*0.005 = 7.57 < 10
    assert t.current_sl == D("63500")


# ------------------------------------------------------------------ trailing, excursions


async def test_trailing_stop_ratchets_and_exits(env: Env) -> None:
    tid = await open_long(env, trailing=TrailingConfig(distance=D("500"), activate_after_r=D("1")))
    await env.tick("65900", bid="65900", ask="65900.1")  # +887 < 1R (1013): not active
    assert env.broker._trades[tid].current_sl == D("64000")
    await env.tick("66100", bid="66100", ask="66100.1")  # +1087 >= 1R
    assert env.broker._trades[tid].current_sl == D("65600")
    await env.tick("65900", bid="65900", ask="65900.1")  # never loosens
    assert env.broker._trades[tid].current_sl == D("65600")
    await env.tick("65599", bid="65599", ask="65599.1")
    t = env.journal.get_trade(tid)
    assert t is not None and t.exit_reason is ExitReason.STOP_LOSS and t.gross_pnl > 0


async def test_trailing_pct(env: Env) -> None:
    tid = await open_long(env, trailing=TrailingConfig(pct=D("1"), activate_after_r=D("0.5")))
    await env.tick("66000", bid="66000", ask="66000.1")
    assert env.broker._trades[tid].current_sl == D("65340")  # 66000 - 1%


async def test_mae_mfe_tracked(env: Env) -> None:
    tid = await open_long(env)
    await env.tick("65500", bid="65500", ask="65500.1")
    await env.tick("64500", bid="64500", ask="64500.1")
    t = env.broker._trades[tid]
    assert t.mfe == D("487") and t.mae == D("513")


# ------------------------------------------------------------------ funding


async def test_funding_charged_to_longs_when_rate_positive(env: Env) -> None:
    tid = await open_long(env)
    before = env.journal.balance(BOOK_ID, Account.MAIN)
    await env.broker.on_funding(
        FundingEvent(instrument_id=BTC, ts=env.clock.now(), rate=D("0.0001"), mark_price=D("65000"))
    )
    t = env.broker._trades[tid]
    assert t.funding == D("-0.03250000")
    assert env.journal.balance(BOOK_ID, Account.MAIN) == before - D("0.0325")
    # funding stamped before the position opened is ignored
    await env.broker.on_funding(
        FundingEvent(
            instrument_id=BTC, ts=env.clock.now() - timedelta(hours=1), rate=D("0.0001"), mark_price=D("65000")
        )
    )
    assert env.broker._trades[tid].funding == D("-0.03250000")
    await env.tick("64000", bid="63999.9", ask="64000")
    closed = env.journal.get_trade(tid)
    assert closed is not None and closed.net_pnl == closed.gross_pnl - closed.charges + D("-0.0325")


async def test_funding_paid_to_shorts(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    _, trade = await env.broker.submit(env.req(direction=Direction.SHORT, sl="66000"))
    assert trade
    await env.tick("65000", **QUOTE)
    await env.broker.on_funding(
        FundingEvent(instrument_id=BTC, ts=env.clock.now(), rate=D("0.0001"), mark_price=D("65000"))
    )
    assert env.broker._trades[trade.id].funding == D("0.03250000")


async def test_funding_ignored_for_unknown_instrument(env: Env) -> None:
    await env.broker.on_funding(FundingEvent(instrument_id="nope", ts=env.clock.now(), rate=D("1"), mark_price=D("1")))


# ------------------------------------------------------------------ risk integration


async def test_rejection_is_logged_and_published(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    d, trade = await env.broker.submit(env.req(qty="0.02"))  # risk 20 > 10
    assert not d.approved and trade is None and d.failed_rule_id == "R006_MAX_RISK_PER_TRADE"
    rows = env.journal.list_risk_decisions(BOOK_ID, rejected_only=True)
    assert rows[0]["failed_rule_id"] == "R006_MAX_RISK_PER_TRADE"
    assert any(t == "risk_decision" and not p["approved"] for t, p in env.events)
    assert any("R006" in a["message"] for a in env.journal.list_audit("RISK"))


async def test_stale_data_blocks_entries(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    env.clock.advance(11)
    d, _ = await env.broker.submit(env.req())
    assert d.failed_rule_id == "R004_STALE_DATA"


async def test_no_averaging_and_trades_per_day(env: Env) -> None:
    await open_long(env)
    d, _ = await env.broker.submit(env.req(sl="64500"))
    assert d.failed_rule_id == "R012_NO_AVERAGING_DOWN"


async def test_max_trades_per_day_counts_across_closed_trades() -> None:
    env = build_env(make_cfg(book_dict(max_trades_per_day=1)))
    tid = await open_long(env)
    await env.broker.close(tid)
    await env.tick("65000", **QUOTE)
    d, _ = await env.broker.submit(env.req())
    assert d.failed_rule_id == "R010_MAX_TRADES_PER_DAY"


async def test_daily_loss_limit_halts_and_flattens_other_positions() -> None:
    env = build_env(
        make_cfg(book_dict(daily_loss_limit_pct=None, daily_loss_limit_abs="12", max_risk_per_trade_pct="1"))
    )
    btc = await open_long(env, qty="0.002")  # risk ~2.1
    await env.tick("3000", bid="2999.9", ask="3000", inst=ETH)
    d, eth = await env.broker.submit(env.req(inst=ETH, qty="0.1", sl="2950"))  # risk ~5.3
    assert d.approved and eth
    await env.tick("3000", bid="2999.9", ask="3000", inst=ETH)
    await env.tick("2800", bid="2800", ask="2800.1", inst=ETH)  # gap: realized loss ~20
    halted, reason = env.broker.halted(BOOK_ID)
    assert halted and reason and "daily loss limit" in reason
    exits = {o.purpose for o in env.broker._orders_for(btc)}
    assert exits == {OrderPurpose.DAILY_LOSS}
    await env.tick("65000", **QUOTE)
    t = env.journal.get_trade(btc)
    assert t is not None and t.exit_reason is ExitReason.DAILY_LOSS
    d2, _ = await env.broker.submit(env.req())
    assert d2.failed_rule_id == "R002_BOOK_HALTED"
    # next UTC day: halt lifts automatically
    env.clock.set(datetime(2026, 1, 6, 0, 0, 1, tzinfo=UTC))
    await env.broker.on_timer()
    assert env.broker.halted(BOOK_ID) == (False, None)


# ------------------------------------------------------------------ session square-off


async def test_intraday_square_off_at_session_end() -> None:
    book = {
        **book_dict(),
        "session": {"timezone": "Asia/Kolkata", "entry_start": "09:20", "entry_end": "15:00", "square_off": "15:15"},
        "trading_day_tz": "Asia/Kolkata",
    }
    env = build_env(make_cfg(book), clock=ReplayClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC)))  # 14:30 IST
    tid = await open_long(env)
    _, pending = await env.broker.submit(
        env.req(inst=ETH, order_type=OrderType.LIMIT, limit_price=D("2000"), sl="1900")
    )
    assert pending is None or pending.status is TradeStatus.PENDING
    env.clock.set(datetime(2026, 1, 5, 9, 31, tzinfo=UTC))  # 15:01 IST: no new entries
    d, _ = await env.broker.submit(env.req(inst=ETH, sl="1900"))
    assert d.failed_rule_id in ("R003_SESSION_HOURS", "R004_STALE_DATA")
    env.clock.set(datetime(2026, 1, 5, 9, 45, tzinfo=UTC))  # 15:15 IST
    await env.broker.on_timer()
    assert {o.purpose for o in env.broker._orders_for(tid)} == {OrderPurpose.SQUARE_OFF}
    await env.tick("65000", **QUOTE)
    t = env.journal.get_trade(tid)
    assert t is not None and t.exit_reason is ExitReason.SQUARE_OFF
    assert not env.broker.active_trades()


# ------------------------------------------------------------------ kill switch


async def test_kill_switch_closes_everything_and_blocks(env: Env) -> None:
    tid = await open_long(env)
    await env.tick("3000", bid="2999.9", ask="3000", inst=ETH)
    _, pend = await env.broker.submit(env.req(inst=ETH, order_type=OrderType.LIMIT, limit_price=D("2900"), sl="2800"))
    assert pend
    res = await env.broker.engage_kill_switch("test")
    assert res["closed"] == 1 and res["cancelled"] == 1
    t = env.journal.get_trade(tid)
    assert t is not None and t.exit_reason is ExitReason.KILL_SWITCH and t.avg_exit == D("64986.9")
    assert not t.stale_exit
    assert not env.broker.active_trades()
    d, _ = await env.broker.submit(env.req())
    assert d.failed_rule_id == "R001_KILL_SWITCH"
    assert env.journal.get_state("kill_switch")["engaged"] is True  # type: ignore[index]
    await env.broker.release_kill_switch()
    assert env.broker.halted(BOOK_ID) == (False, None)
    await env.tick("65000", **QUOTE)
    d, _ = await env.broker.submit(env.req())
    assert d.approved


async def test_kill_switch_with_stale_data_flags_exit(env: Env) -> None:
    tid = await open_long(env)
    env.clock.advance(60)
    await env.broker.engage_kill_switch("stale test")
    t = env.journal.get_trade(tid)
    assert t is not None and t.stale_exit
    fills = env.journal.fills_for_trade(tid)
    assert fills[-1]["stale_price"] is True


async def test_kill_switch_state_survives_restart(env: Env) -> None:
    await env.broker.engage_kill_switch("persist")
    b2 = PaperBroker(env.cfg, env.clock, env.bus, env.journal, env.registry, env.market)
    b2.start()
    assert b2.kill_switch_engaged


# ------------------------------------------------------------------ expiry / recovery


async def test_expiry_settlement() -> None:
    env = build_env()
    inst = env.registry.get(BTC).model_copy(update={"expiry": datetime(2026, 1, 5, 10, 30, tzinfo=UTC)})
    env.registry.upsert([inst], env.clock.now())
    env.broker.settlement_price = lambda t, i: D("65500")
    tid = await open_long(env)
    env.clock.set(datetime(2026, 1, 5, 10, 30, tzinfo=UTC))
    await env.broker.on_timer()
    t = env.journal.get_trade(tid)
    assert t is not None and t.exit_reason is ExitReason.EXPIRY and t.avg_exit == D("65500")


async def test_recovery_after_restart_keeps_stops_working(env: Env) -> None:
    tid = await open_long(env)
    b2 = PaperBroker(env.cfg, env.clock, env.bus, env.journal, env.registry, env.market)
    b2.start()
    assert tid in b2._trades
    assert {o.purpose for o in b2._orders_for(tid)} == {OrderPurpose.STOP}
    env.broker = b2
    await env.tick("64000", bid="63999.9", ask="64000")
    t = env.journal.get_trade(tid)
    assert t is not None and t.status is TradeStatus.CLOSED


# ------------------------------------------------------------------ views


async def test_book_summary_and_trade_view(env: Env) -> None:
    tid = await open_long(env)
    await env.tick("65500", bid="65500", ask="65500.1")
    s = env.broker.book_summary(BOOK_ID)
    assert s["balance"] == "999.83746750"
    assert s["unrealized"] == "2.43500000"  # (65500 - 65013) * 0.005
    assert s["equity"] == "1002.27246750"
    assert s["used_margin"] == "325.06500000"
    pos = s["positions"][0]
    assert pos["id"] == tid and pos["symbol"] == "BTC/USDT:USDT"
    # net after fees: 2.435 - entry fees 0.1625325 - est exit fees (0.005*65500*0.05% = 0.16375)
    assert pos["unrealized_net"] == "2.10871750"
    assert pos["unrealized_r"] == "0.42"
    assert D(s["today_pnl"]) == D("-0.1625325") + D("2.435") - D("0.16375")


async def test_preview_contains_sizing(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    p = env.broker.preview(env.req(target="67000"))
    assert p["decision"]["approved"] is True
    assert p["max_qty_by_risk"] == "0.009"
    assert p["est_entry"] == "65000"
    assert p["reward_risk"] is not None


async def test_actor_is_recorded(env: Env) -> None:
    await env.tick("65000", **QUOTE)
    _, trade = await env.broker.submit(env.req(actor=Actor.AGENT))
    assert trade and trade.actor is Actor.AGENT


async def test_ledger_is_append_only_sum(env: Env) -> None:
    await open_long(env)
    kinds = []
    from sqlalchemy import select

    from papermind.db.models import LedgerEntryRow

    with env.db.session() as s:
        rows = s.scalars(select(LedgerEntryRow)).all()
        kinds = [r.kind for r in rows]
        total = sum((r.amount for r in rows), D(0))
    assert kinds[0] == str(LedgerKind.DEPOSIT)
    assert total == env.journal.balance(BOOK_ID, Account.MAIN)


async def test_order_statuses_persisted(env: Env) -> None:
    tid = await open_long(env, target="66000")
    await env.tick("64000", bid="63999.9", ask="64000")
    from sqlalchemy import select

    from papermind.db.models import OrderRow

    with env.db.session() as s:
        st = {r.purpose: r.status for r in s.scalars(select(OrderRow).where(OrderRow.trade_id == tid))}
    assert st == {
        "entry": str(OrderStatus.FILLED),
        "stop": str(OrderStatus.FILLED),
        "target": str(OrderStatus.CANCELLED),
    }
