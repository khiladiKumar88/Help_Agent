"""Mode routing: Manual / Co-pilot / Auto, card expiry, and the shadow accounts.

Everything goes through the real AgentExecutor, PaperBroker and risk rules — the only stand-in
is the LLM provider.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from papermind.core.config import Mode
from papermind.core.events import EventBus
from papermind.core.types import Account, Actor, Direction, Signal
from papermind.llm.base import LLMTimeout
from papermind.llm.fake import FakeProvider
from papermind.scanner.agent import CARD_PENDING, AgentExecutor, CardError, RuleDecider
from tests.analyst.conftest import Rig, build_rig, reply

D = Decimal
QUOTE = {"bid": "64999.9", "ask": "65000"}


def executor(rig: Rig, shadow_raw: bool = True) -> AgentExecutor:
    """Executor wired to the rig's broker/journal, with the rig's analyst as the decider."""
    return AgentExecutor(
        rig.env.bus,
        rig.env.broker,
        rig.env.journal,
        rig.analyst,
        force_execute=False,
        clock=rig.env.clock,
        shadow_raw=shadow_raw,
    )


async def tick(rig: Rig, price: str = "65000") -> None:
    await rig.env.tick(price, **QUOTE)


def open_trades(rig: Rig, account: Account) -> list:
    return [t for t in rig.env.journal.list_trades(rig.env.broker.book("crypto-futures-intraday").id, account)]


class TestModeRouting:
    async def test_manual_mode_places_nothing(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.MANUAL.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        assert rig.row(sig.id)["decision"] == "take"  # analysis still happens
        assert rig.row(sig.id)["card_status"] == "none"
        assert open_trades(rig, Account.MAIN) == []

    async def test_auto_mode_places_the_trade(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        row = rig.row(sig.id)
        assert row["executed"] is True and row["trade_id"]
        trades = open_trades(rig, Account.MAIN)
        assert len(trades) == 1 and trades[0].actor is Actor.AGENT

    async def test_a_skip_places_nothing_even_in_auto(self, rig: Rig) -> None:
        rig.provider.replies = [reply("SKIP", 95)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        await ex.on_signal(rig.signal())
        assert open_trades(rig, Account.MAIN) == []

    async def test_an_llm_failure_places_nothing_in_auto(self) -> None:
        """The headline fail-safe: a broken LLM must not become a trade."""
        rig = build_rig(FakeProvider(fail_with=LLMTimeout("too slow")))
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        assert open_trades(rig, Account.MAIN) == []
        assert rig.row(sig.id)["decision"] == "skip"

    async def test_copilot_mode_creates_a_pending_card_instead_of_trading(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.COPILOT.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        row = rig.row(sig.id)
        assert row["card_status"] == CARD_PENDING and row["card_expires_at"] is not None
        assert open_trades(rig, Account.MAIN) == [], "a card must not trade before you answer"
        assert [r["id"] for r in rig.env.journal.pending_cards()] == [sig.id]


class TestCards:
    async def pending(self, rig: Rig) -> tuple[AgentExecutor, Signal]:
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.COPILOT.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        return ex, sig

    async def test_approving_places_the_trade(self, rig: Rig) -> None:
        ex, sig = await self.pending(rig)
        out = await ex.resolve_card(sig.id, approve=True)
        assert out["card_status"] == "approved" and out["trade_id"]
        row = rig.row(sig.id)
        assert row["card_status"] == "approved" and row["card_decided_at"] is not None
        assert row["executed"] is True
        assert len(open_trades(rig, Account.MAIN)) == 1

    async def test_rejecting_shadow_tracks_instead_of_trading(self, rig: Rig) -> None:
        ex, sig = await self.pending(rig)
        out = await ex.resolve_card(sig.id, approve=False)
        assert out["card_status"] == "rejected"
        assert open_trades(rig, Account.MAIN) == [], "a rejected card must not touch the real book"
        # ...but we record what it would have done, which is the learning data
        assert out["shadow_trade_id"]
        assert rig.row(sig.id)["rejected_trade_id"] == out["shadow_trade_id"]
        assert len(open_trades(rig, Account.SHADOW_REJECTED)) == 1

    async def test_a_card_cannot_be_answered_twice(self, rig: Rig) -> None:
        ex, sig = await self.pending(rig)
        await ex.resolve_card(sig.id, approve=True)
        with pytest.raises(CardError, match="already approved"):
            await ex.resolve_card(sig.id, approve=True)

    async def test_an_unknown_card_is_refused(self, rig: Rig) -> None:
        ex = executor(rig)
        with pytest.raises(CardError, match="unknown"):
            await ex.resolve_card("sig_nope", approve=True)

    async def test_a_card_expires_after_its_ttl(self, rig: Rig) -> None:
        ex, sig = await self.pending(rig)
        rig.env.clock.advance(seconds=15 * 60 + 1)  # one 15m bar is the default TTL
        assert await ex.expire_cards() == 1
        row = rig.row(sig.id)
        assert row["card_status"] == "expired" and row["card_decided_at"] is not None
        assert rig.env.journal.pending_cards() == []

    async def test_a_card_does_not_expire_early(self, rig: Rig) -> None:
        ex, _sig = await self.pending(rig)
        rig.env.clock.advance(seconds=60)
        assert await ex.expire_cards() == 0

    async def test_an_expired_card_cannot_be_approved(self, rig: Rig) -> None:
        ex, sig = await self.pending(rig)
        rig.env.clock.advance(seconds=15 * 60 + 1)
        with pytest.raises(CardError, match="expired"):
            await ex.resolve_card(sig.id, approve=True)
        assert rig.row(sig.id)["card_status"] == "expired"
        assert open_trades(rig, Account.MAIN) == []

    async def test_the_ttl_comes_from_config(self) -> None:
        from tests.conftest import book_dict, make_cfg

        book = book_dict()
        book["copilot"] = {"card_ttl_seconds": 30}
        rig = build_rig(cfg=make_cfg(book=book, llm={"provider": "fake", "model": "m"}))
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode(book["id"], Mode.COPILOT.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        rig.env.clock.advance(seconds=31)
        assert await ex.expire_cards() == 1


class TestShadowAccounts:
    async def test_every_signal_is_traded_in_shadow_raw(self, rig: Rig) -> None:
        """The control group: raw_strategy takes signals the analyst skipped."""
        rig.provider.replies = [reply("SKIP", 95)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=True)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        assert open_trades(rig, Account.MAIN) == [], "the analyst skipped it"
        raw = open_trades(rig, Account.SHADOW_RAW)
        assert len(raw) == 1, "but the unfiltered strategy still took it"
        assert rig.row(sig.id)["raw_trade_id"] == raw[0].id

    async def test_shadow_raw_runs_even_when_the_llm_fails(self) -> None:
        rig = build_rig(FakeProvider(fail_with=LLMTimeout("x")))
        ex = executor(rig, shadow_raw=True)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        await ex.on_signal(rig.signal())
        assert len(open_trades(rig, Account.SHADOW_RAW)) == 1
        assert open_trades(rig, Account.MAIN) == []

    async def test_shadow_raw_is_off_when_nothing_filters(self, rig: Rig) -> None:
        """With the rules decider the agent IS the raw strategy, so there is nothing to compare."""
        ex = AgentExecutor(
            EventBus(), rig.env.broker, rig.env.journal, RuleDecider(), clock=rig.env.clock, shadow_raw=False
        )
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        assert open_trades(rig, Account.SHADOW_RAW) == []
        assert len(open_trades(rig, Account.MAIN)) == 1

    async def test_the_shadow_wallets_are_funded_separately(self, rig: Rig) -> None:
        book = rig.env.book
        for account in (Account.MAIN, Account.SHADOW_BASELINE, Account.SHADOW_RAW, Account.SHADOW_REJECTED):
            assert rig.env.journal.balance(book.id, account) == book.starting_capital


class TestAnalystAdjustments:
    async def test_a_tighter_stop_reaches_the_broker(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 90, suggested_adjustments={"stop_loss": "64500"})]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        sig = rig.signal()
        await ex.on_signal(sig)
        trades = open_trades(rig, Account.MAIN)
        assert len(trades) == 1 and trades[0].initial_sl == D("64500")

    async def test_a_shrink_factor_reduces_the_size(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 90)]  # type: ignore[attr-defined]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        await ex.on_signal(rig.signal(id="sig_full"))
        full = open_trades(rig, Account.MAIN)[0].qty

        rig2 = build_rig()
        rig2.provider.replies = [reply("TAKE", 90, suggested_adjustments={"size_factor": 0.5})]  # type: ignore[attr-defined]
        ex2 = executor(rig2, shadow_raw=False)
        rig2.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig2)
        await ex2.on_signal(rig2.signal(id="sig_half"))
        half = open_trades(rig2, Account.MAIN)[0].qty
        assert half < full and half > 0

    async def test_a_direction_aware_short_stop_is_honoured(self, rig: Rig) -> None:
        rig.provider.replies = [reply("TAKE", 90, suggested_adjustments={"stop_loss": "65500"})]
        ex = executor(rig, shadow_raw=False)
        rig.env.broker.set_mode("crypto-futures-intraday", Mode.AUTO.value)
        await tick(rig)
        sig = rig.signal(direction=Direction.SHORT, stop_loss=D("66000"), target=D("63000"))
        await ex.on_signal(sig)
        trades = open_trades(rig, Account.MAIN)
        assert len(trades) == 1 and trades[0].initial_sl == D("65500")
