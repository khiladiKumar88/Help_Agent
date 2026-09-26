"""Every risk rule: a passing case and each failing branch."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import pytest

from papermind.core.config import BookConfig, Segment, SessionConfig
from papermind.core.types import (
    Actor,
    Direction,
    InstrumentKind,
    OrderRequest,
    OrderType,
    Tick,
)
from papermind.risk.manager import RiskManager
from papermind.risk.rules import RULES, OpenPosition, RiskContext
from tests.conftest import BOOK_ID, BTC, T0, book_dict, ccxt_instruments

D = Decimal


def book(**risk: Any) -> BookConfig:
    return BookConfig.model_validate(book_dict(**risk))


def ctx(**kw: Any) -> RiskContext:
    inst = ccxt_instruments()[0]
    base: dict[str, Any] = dict(
        now=T0,
        book=book(),
        instrument=inst,
        tick=Tick(instrument_id=BTC, ts=T0, ltp=D("65000"), bid=D("64999.9"), ask=D("65000")),
        stale=False,
        kill_switch=False,
        book_halted=False,
        halt_reason=None,
        balance=D("1000"),
        equity=D("1000"),
        used_margin=D("0"),
        day_start_equity=D("1000"),
        day_pnl=D("0"),
        trades_today=0,
        positions=[],
        round_trip_charges=lambda q, e, x: (q * e + q * x) * D("0.0005"),
    )
    base.update(kw)
    return RiskContext(**base)


def req(**kw: Any) -> OrderRequest:
    base: dict[str, Any] = dict(
        book_id=BOOK_ID,
        actor=Actor.HUMAN,
        instrument_id=BTC,
        direction=Direction.LONG,
        qty=D("0.005"),
        stop_loss=D("64000"),
        leverage=D("1"),
    )
    base.update(kw)
    return OrderRequest(**base)


RM = RiskManager()


def rejected_by(c: RiskContext, r: OrderRequest) -> str | None:
    return RM.evaluate(c, r).failed_rule_id


def test_happy_path_all_rules_pass() -> None:
    d = RM.evaluate(ctx(), req())
    assert d.approved, d.message
    assert len(d.results) == len(RULES)
    # risk = (65000 - 64000) * 0.005 + charges ((0.005*65000 + 0.005*64000) * 0.0005 = 0.3225)
    assert d.risk_amount == D("5.3225")


@pytest.mark.parametrize(
    ("r", "msg"),
    [
        (req(book_id="other-book"), "order is for book"),
        (req(instrument_id="crypto:binanceusdm:DOGE/USDT:USDT"), "unknown instrument"),
        (req(qty=D("0.0001")), "below minimum"),
        (req(qty=D("0.0015")), "not a multiple of step"),
        (req(leverage=D("0.5")), "leverage must be >= 1"),
        (req(order_type=OrderType.LIMIT), "limit order needs"),
        (req(order_type=OrderType.STOP_MARKET), "not supported"),
        (req(order_type=OrderType.LIMIT, limit_price=D("65000.05")), "tick size"),
    ],
)
def test_r000_order_validation(r: OrderRequest, msg: str) -> None:
    c = ctx(instrument=None) if "DOGE" in r.instrument_id else ctx()
    d = RM.evaluate(c, r)
    assert d.failed_rule_id == "R000_ORDER_VALID"
    assert msg in d.message


def test_r000_symbol_not_in_book_and_inactive_and_segment() -> None:
    inst = ccxt_instruments()[0]
    assert rejected_by(ctx(instrument=inst.model_copy(update={"symbol": "SOL/USDT:USDT"})), req()) == "R000_ORDER_VALID"
    assert rejected_by(ctx(instrument=inst.model_copy(update={"active": False})), req()) == "R000_ORDER_VALID"
    spot = inst.model_copy(update={"segment": Segment.SPOT})
    assert "segment" in RM.evaluate(ctx(instrument=spot), req()).message


def test_r000_no_shorts_or_leverage_on_spot_and_options() -> None:
    inst = ccxt_instruments()[0]
    spot_book = BookConfig.model_validate({**book_dict(), "segment": "spot", "id": "crypto-spot-intraday"})
    spot = inst.model_copy(update={"segment": Segment.SPOT, "kind": InstrumentKind.SPOT})
    c = ctx(book=spot_book, instrument=spot)
    r = req(book_id="crypto-spot-intraday", direction=Direction.SHORT, stop_loss=D("66000"))
    assert "short selling" in RM.evaluate(c, r).message
    r2 = req(book_id="crypto-spot-intraday", leverage=D("2"))
    assert "leverage must be 1" in RM.evaluate(c, r2).message


def test_r001_kill_switch() -> None:
    assert rejected_by(ctx(kill_switch=True), req()) == "R001_KILL_SWITCH"


def test_r002_halted_and_disabled() -> None:
    assert rejected_by(ctx(book_halted=True, halt_reason="x"), req()) == "R002_BOOK_HALTED"
    disabled = BookConfig.model_validate({**book_dict(), "enabled": False})
    assert rejected_by(ctx(book=disabled), req()) == "R002_BOOK_HALTED"


def _india_session_book() -> BookConfig:
    return BookConfig.model_validate(
        {
            **book_dict(),
            "session": {
                "timezone": "Asia/Kolkata",
                "entry_start": "09:20",
                "entry_end": "15:00",
                "square_off": "15:15",
                "holidays": ["2026-01-26"],
            },
        }
    )


@pytest.mark.parametrize(
    ("utc", "ok", "msg"),
    [
        (datetime(2026, 1, 5, 3, 45, tzinfo=UTC), False, "entries open at 09:20"),  # 09:15 IST
        (datetime(2026, 1, 5, 3, 50, tzinfo=UTC), True, "within"),  # 09:20 IST
        (datetime(2026, 1, 5, 9, 29, tzinfo=UTC), True, "within"),  # 14:59 IST
        (datetime(2026, 1, 5, 9, 30, tzinfo=UTC), False, "no new entries after 15:00"),  # 15:00 IST
        (datetime(2026, 1, 4, 6, 0, tzinfo=UTC), False, "not a trading day"),  # Sunday
        (datetime(2026, 1, 26, 6, 0, tzinfo=UTC), False, "not a trading day"),  # holiday
    ],
)
def test_r003_session_hours(utc: datetime, ok: bool, msg: str) -> None:
    res = RULES[3](ctx(book=_india_session_book(), now=utc), req())
    assert res.rule_id == "R003_SESSION_HOURS"
    assert res.passed is ok
    assert msg in res.message


def test_r003_crypto_is_24x7() -> None:
    assert RULES[3](ctx(now=datetime(2026, 1, 4, 3, 0, tzinfo=UTC)), req()).passed


def test_r004_stale_or_missing_data() -> None:
    assert rejected_by(ctx(stale=True), req()) == "R004_STALE_DATA"
    assert rejected_by(ctx(tick=None), req()) == "R004_STALE_DATA"


@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"stop_loss": None}, "required"),
        ({"stop_loss": D("0")}, "positive"),
        ({"stop_loss": D("64000.05")}, "tick size"),
        ({"stop_loss": D("65500")}, "must be below entry"),
        ({"direction": Direction.SHORT, "stop_loss": D("64000")}, "must be above entry"),
        ({"target": D("64500")}, "must be above entry"),
        ({"target": D("66000.03")}, "tick size"),
        ({"direction": Direction.SHORT, "stop_loss": D("66000"), "target": D("66500")}, "must be below entry"),
    ],
)
def test_r005_stop_loss_and_target(kw: dict[str, Any], msg: str) -> None:
    d = RM.evaluate(ctx(), req(**kw))
    assert d.failed_rule_id == "R005_STOP_LOSS_VALID"
    assert msg in d.message


def test_r005_uses_limit_price_for_limit_orders() -> None:
    r = req(order_type=OrderType.LIMIT, limit_price=D("63000"), stop_loss=D("63500"))
    assert rejected_by(ctx(), r) == "R005_STOP_LOSS_VALID"
    ok = req(order_type=OrderType.LIMIT, limit_price=D("63000"), stop_loss=D("62500"))
    assert RM.evaluate(ctx(), ok).approved


def test_r005_short_uses_bid_and_no_quote_falls_back_to_ltp() -> None:
    c = ctx(tick=Tick(instrument_id=BTC, ts=T0, ltp=D("65000")))
    assert RM.evaluate(c, req(direction=Direction.SHORT, stop_loss=D("65500"))).approved


def test_r006_max_risk_per_trade_pct_and_abs() -> None:
    # 1% of 1000 = 10 USDT; 0.01 BTC with 1000 stop = 10 + charges > 10
    d = RM.evaluate(ctx(), req(qty=D("0.01")))
    assert d.failed_rule_id == "R006_MAX_RISK_PER_TRADE"
    assert "incl. est. charges" in d.message
    c = ctx(book=book(max_risk_per_trade_pct=None, max_risk_per_trade_abs="4"))
    assert rejected_by(c, req()) == "R006_MAX_RISK_PER_TRADE"
    c2 = ctx(book=book(max_risk_per_trade_pct=None, max_risk_per_trade_abs="6"))
    assert RM.evaluate(c2, req()).approved


def test_r006_uses_smaller_of_abs_and_pct() -> None:
    c = ctx(book=book(max_risk_per_trade_pct="1", max_risk_per_trade_abs="3"))
    assert c.max_risk_allowed() == D("3")


def test_r007_max_lots_and_qty() -> None:
    assert rejected_by(ctx(book=book(max_qty="0.004")), req()) == "R007_MAX_SIZE"
    inst = ccxt_instruments()[0].model_copy(update={"lot_size": D("0.001")})
    assert rejected_by(ctx(book=book(max_lots=4), instrument=inst), req()) == "R007_MAX_SIZE"
    assert RM.evaluate(ctx(book=book(max_lots=5), instrument=inst), req()).approved


def test_r008_max_premium_only_for_options() -> None:
    inst = ccxt_instruments()[0].model_copy(update={"kind": InstrumentKind.CE})
    c = ctx(book=book(max_premium="150"), instrument=inst)
    res = RULES[8](c, req())
    assert res.rule_id == "R008_MAX_PREMIUM" and not res.passed
    cheap = replace(c, tick=Tick(instrument_id=BTC, ts=T0, ltp=D("120"), bid=D("119"), ask=D("120")))
    assert RULES[8](cheap, req(stop_loss=D("100"))).passed
    assert RULES[8](ctx(book=book(max_premium="150")), req()).passed  # perp: not applicable


def test_r009_max_open_positions() -> None:
    pos = [
        OpenPosition("t1", "x", Direction.LONG, D("1"), "open"),
        OpenPosition("t2", "y", Direction.LONG, D("1"), "open"),
    ]
    assert rejected_by(ctx(positions=pos), req()) == "R009_MAX_OPEN_POSITIONS"
    assert RM.evaluate(ctx(positions=pos[:1]), req()).approved


def test_r010_max_trades_per_day() -> None:
    assert rejected_by(ctx(trades_today=10), req()) == "R010_MAX_TRADES_PER_DAY"
    assert RM.evaluate(ctx(trades_today=9), req()).approved


def test_r011_daily_loss_limit() -> None:
    # limit = 3% of 1000 = 30
    assert rejected_by(ctx(day_pnl=D("-30")), req()) == "R011_DAILY_LOSS_LIMIT"
    d = RM.evaluate(ctx(day_pnl=D("-26")), req())  # -26 - 5.32 < -30
    assert d.failed_rule_id == "R011_DAILY_LOSS_LIMIT" and "would breach" in d.message
    assert RM.evaluate(ctx(day_pnl=D("-24")), req()).approved
    assert RM.evaluate(ctx(book=book(daily_loss_limit_pct=None), day_pnl=D("-500")), req()).approved


def test_r011_abs_limit() -> None:
    c = ctx(book=book(daily_loss_limit_pct=None, daily_loss_limit_abs="10"), day_pnl=D("-5"))
    assert rejected_by(c, req()) == "R011_DAILY_LOSS_LIMIT"


def test_r012_no_averaging_or_adding() -> None:
    pos = [OpenPosition("t1", BTC, Direction.LONG, D("0.005"), "open")]
    assert rejected_by(ctx(positions=pos), req()) == "R012_NO_AVERAGING_DOWN"
    pending = [OpenPosition("t1", BTC, Direction.SHORT, D("0.005"), "pending")]
    assert rejected_by(ctx(positions=pending), req()) == "R012_NO_AVERAGING_DOWN"


def test_r013_max_leverage() -> None:
    assert rejected_by(ctx(), req(leverage=D("5"))) == "R013_MAX_LEVERAGE"
    assert RM.evaluate(ctx(), req(leverage=D("3"))).approved


def test_r014_margin() -> None:
    # 0.005 * 65000 = 325 notional at 1x; only 300 available
    assert rejected_by(ctx(used_margin=D("700")), req()) == "R014_SUFFICIENT_MARGIN"
    assert RM.evaluate(ctx(used_margin=D("700")), req(leverage=D("2"))).approved


def test_crashing_rule_is_a_rejection() -> None:
    def boom(q: Decimal, e: Decimal, x: Decimal) -> Decimal:
        raise RuntimeError("boom")

    d = RM.evaluate(ctx(round_trip_charges=boom), req())
    assert not d.approved and "rule error" in d.message


def test_require_stop_loss_cannot_be_disabled() -> None:
    with pytest.raises(ValueError, match="cannot be disabled"):
        book(require_stop_loss=False)


def test_max_qty_for_risk() -> None:
    q = RM.max_qty_for_risk(ctx(), req(qty=D("1")))
    c = ctx()
    assert c.risk_amount(req(), q) <= D("10")  # type: ignore[operator]
    assert c.risk_amount(req(), q + D("0.001")) > D("10")  # type: ignore[operator]
    assert q == D("0.009")


def test_max_qty_for_risk_edge_cases() -> None:
    assert RM.max_qty_for_risk(ctx(instrument=None), req()) == 0
    assert RM.max_qty_for_risk(ctx(), req(stop_loss=None)) == 0
    assert RM.max_qty_for_risk(ctx(book=book(max_risk_per_trade_pct=None)), req()) == 0
    assert RM.max_qty_for_risk(ctx(book=book(max_risk_per_trade_abs="0.01", max_risk_per_trade_pct=None)), req()) == 0


def test_session_config_holidays_loaded_types() -> None:
    s = SessionConfig(entry_start=time(9, 20), entry_end=time(15), holidays=[date(2026, 1, 26)])
    assert s.tz.key == "Asia/Kolkata"
