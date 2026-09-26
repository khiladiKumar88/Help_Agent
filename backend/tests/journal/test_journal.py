from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from papermind.core.types import Account, LedgerKind
from tests.conftest import BOOK_ID, T0, Env, build_env

D = Decimal


def test_book_created_once_with_deposit(env: Env) -> None:
    env.journal.ensure_book(env.book)  # idempotent
    assert env.journal.balance(BOOK_ID, Account.MAIN) == D("1000")
    assert env.journal.book_state(BOOK_ID)["mode"] == "manual"
    with pytest.raises(KeyError):
        env.journal.book_state("nope")
    with pytest.raises(KeyError):
        env.journal.set_book_halt("nope", True, "x", None)
    with pytest.raises(KeyError):
        env.journal.set_book_mode("nope", "auto")


def test_balances_rebuilt_from_ledger_on_restart(env: Env) -> None:
    env.journal.add_ledger(BOOK_ID, Account.MAIN, LedgerKind.CHARGE, D("-0.5"))
    env.journal.add_ledger(BOOK_ID, Account.SHADOW_BASELINE, LedgerKind.DEPOSIT, D("1000"))
    from papermind.journal.service import Journal

    j2 = Journal(env.db, env.clock)
    assert j2.balance(BOOK_ID, Account.MAIN) == D("999.5")
    assert j2.balance(BOOK_ID, Account.SHADOW_BASELINE) == D("1000")


def test_pnl_since_cache_tracks_new_entries(env: Env) -> None:
    since = T0 - timedelta(hours=1)
    assert env.journal.pnl_since(BOOK_ID, Account.MAIN, since) == 0  # deposit excluded
    env.journal.add_ledger(BOOK_ID, Account.MAIN, LedgerKind.REALIZED_PNL, D("5"))
    env.journal.add_ledger(BOOK_ID, Account.MAIN, LedgerKind.CHARGE, D("-1"))
    assert env.journal.pnl_since(BOOK_ID, Account.MAIN, since) == D("4")
    fresh = build_env(db=env.db, clock=env.clock)
    assert fresh.journal.pnl_since(BOOK_ID, Account.MAIN, since) == D("4")


def test_state_and_audit(env: Env) -> None:
    assert env.journal.get_state("x") is None
    env.journal.set_state("x", {"a": 1})
    env.journal.set_state("x", {"a": 2})
    assert env.journal.get_state("x") == {"a": 2}
    env.journal.audit("SYSTEM", "api_key=abcdef123 used", payload={"secret": "s"})
    row = env.journal.list_audit("SYSTEM", limit=1)[0]
    assert "abcdef123" not in row["message"] and row["payload"]["secret"] != "s"
    env.journal.set_book_mode(BOOK_ID, "copilot")
    assert env.journal.book_state(BOOK_ID)["mode"] == "copilot"


def test_audit_failure_never_raises(env: Env) -> None:
    env.db.engine.dispose()
    env.journal.db = None  # type: ignore[assignment]
    env.journal.audit("SYSTEM", "still fine")  # swallowed + logged


def test_equity_curve_and_filters(env: Env) -> None:
    env.journal.snapshot_equity(BOOK_ID, Account.MAIN, D("1000"), D("5"))
    env.clock.advance(60)
    env.journal.snapshot_equity(BOOK_ID, Account.MAIN, D("1001"), D("0"))
    curve = env.journal.equity_curve(BOOK_ID, Account.MAIN)
    assert [c["equity"] for c in curve] == [D("1005"), D("1001")]
    assert len(env.journal.equity_curve(BOOK_ID, Account.MAIN, since=env.clock.now())) == 1
    assert env.journal.list_trades(BOOK_ID, Account.MAIN, status="open", actor="human", since=T0) == []
    assert env.journal.get_trade("nope") is None
