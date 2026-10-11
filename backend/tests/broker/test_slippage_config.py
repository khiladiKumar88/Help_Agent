"""Per-instrument slippage: the model's assumption, resolved per symbol.

The book-wide default is deliberately wide, so an instrument nobody has measured costs MORE
rather than less. A per-symbol entry carries the measurement it was calibrated against, the way
the fee files carry `source_url` / `as_of`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from papermind.broker.fills import market_fill
from papermind.core.config import InstrumentSlippage, SlippageConfig
from papermind.core.types import Side, Tick
from tests.conftest import BOOK_ID, BTC, T0, book_dict, build_env, ccxt_instruments, make_cfg

D = Decimal


def cfg_with(per_instrument: dict[str, dict[str, object]]) -> SlippageConfig:
    return SlippageConfig.model_validate(
        {"model": "bps", "bps": "2", "est_spread_bps": "2", "per_instrument": per_instrument}
    )


class TestResolution:
    def test_an_unlisted_symbol_falls_back_to_the_book_default(self) -> None:
        c = cfg_with({"BTC/USDT:USDT": {"bps": "0.4"}})
        eff = c.for_symbol("SOL/USDT:USDT")
        assert eff.bps == D("2") and eff.est_spread_bps == D("2")
        assert eff is c, "no override means the same object, not a copy"

    def test_a_listed_symbol_uses_its_own_numbers(self) -> None:
        c = cfg_with({"BTC/USDT:USDT": {"bps": "0.4", "est_spread_bps": "0.02"}})
        eff = c.for_symbol("BTC/USDT:USDT")
        assert eff.bps == D("0.4") and eff.est_spread_bps == D("0.02")

    def test_a_partial_override_keeps_the_rest_of_the_book_default(self) -> None:
        """An entry may tune one number only."""
        c = cfg_with({"BTC/USDT:USDT": {"bps": "0.4"}})
        eff = c.for_symbol("BTC/USDT:USDT")
        assert eff.bps == D("0.4")
        assert eff.est_spread_bps == D("2"), "not overridden, so the book value stands"

    def test_the_model_itself_is_never_overridden_per_symbol(self) -> None:
        c = cfg_with({"BTC/USDT:USDT": {"extra_ticks": 3}})
        assert c.for_symbol("BTC/USDT:USDT").model == "bps"

    def test_per_side_bps_adds_half_the_spread_to_the_slippage_term(self) -> None:
        c = cfg_with({"BTC/USDT:USDT": {"bps": "0.4", "est_spread_bps": "0.02"}})
        assert c.per_side_bps("BTC/USDT:USDT") == D("0.41")  # 0.02/2 + 0.4
        assert c.per_side_bps("SOL/USDT:USDT") == D("3")  # 2/2 + 2


class TestProvenance:
    def test_an_entry_can_record_what_was_measured(self) -> None:
        e = InstrumentSlippage.model_validate(
            {
                "bps": "0.4",
                "est_spread_bps": "0.02",
                "observed_spread_bps": "0.012",
                "source": "binanceusdm order book",
                "as_of": "2026-10-11",
                "notes": "25k clip did not clear level 1",
            }
        )
        assert e.observed_spread_bps == D("0.012")
        assert e.as_of == date(2026, 10, 11) and "level 1" in e.notes

    def test_the_assumption_should_be_worse_than_the_observation(self) -> None:
        """Not enforced by code, but the shipped config must hold to it."""
        from papermind.core.config import load_config

        book = load_config().book(BOOK_ID)
        for symbol, entry in book.slippage.per_instrument.items():
            if entry.observed_spread_bps is None:
                continue
            assumed = book.slippage.per_side_bps(symbol)
            observed_half = entry.observed_spread_bps / 2
            assert assumed > observed_half, (
                f"{symbol}: assumes {assumed} bps per side but observed {observed_half} — "
                "the assumption must be pessimistic, not optimistic"
            )

    def test_every_calibrated_entry_says_where_the_number_came_from(self) -> None:
        from papermind.core.config import load_config

        for book in load_config().books.values():
            for symbol, entry in book.slippage.per_instrument.items():
                assert entry.source, f"{symbol} has no source"
                assert entry.as_of is not None, f"{symbol} has no as_of date"


class TestAppliedToFills:
    def test_the_fill_uses_the_symbol_s_own_slippage(self) -> None:
        btc = ccxt_instruments()[0]
        tick = Tick(instrument_id=BTC, ts=T0, ltp=D("65000"), bid=D("64999.9"), ask=D("65000"))
        wide = market_fill(btc, Side.BUY, tick, cfg_with({}).for_symbol(btc.symbol))
        tight = market_fill(btc, Side.BUY, tick, cfg_with({btc.symbol: {"bps": "0.4"}}).for_symbol(btc.symbol))
        assert wide.price == D("65013")  # 2 bps
        assert tight.price == D("65002.6")  # 0.4 bps
        assert tight.slippage < wide.slippage

    def test_the_broker_resolves_it_per_instrument(self) -> None:
        book = book_dict()
        book["slippage"] = {
            "model": "bps",
            "bps": "2",
            "est_spread_bps": "2",
            "per_instrument": {"BTC/USDT:USDT": {"bps": "0.4", "est_spread_bps": "0.02"}},
        }
        env = build_env(make_cfg(book))
        btc = env.broker.slippage_for(BOOK_ID, BTC)
        eth = env.broker.slippage_for(BOOK_ID, "crypto:binanceusdm:ETH/USDT:USDT")
        assert btc.bps == D("0.4") and btc.est_spread_bps == D("0.02")
        assert eth.bps == D("2"), "ETH has no entry here, so it keeps the wide default"

    async def test_a_cheaper_instrument_really_fills_better(self) -> None:
        """End to end through submit/on_tick, not just the config object."""
        book = book_dict()
        book["slippage"] = {
            "model": "bps",
            "bps": "2",
            "est_spread_bps": "2",
            "per_instrument": {"BTC/USDT:USDT": {"bps": "0.1", "est_spread_bps": "0.02"}},
        }
        env = build_env(make_cfg(book))
        await env.tick("65000", bid="64999.9", ask="65000")
        _d, trade = await env.broker.submit(env.req())
        assert trade is not None
        await env.tick("65000", bid="64999.9", ask="65000")
        view = env.broker.trade_view(trade)
        # 0.1 bps on a 65000 ask = 0.65, rounded up to the 0.1 tick
        assert D(view["avg_entry"]) == D("65000.7")

    def test_an_unmeasured_instrument_is_not_quietly_made_cheap(self) -> None:
        from papermind.core.config import load_config

        book = load_config().book(BOOK_ID)
        assert book.slippage.bps >= D("2"), "the book-wide fallback must stay pessimistic"
        assert book.slippage.est_spread_bps >= D("2")


class TestPreviewShowsTheResolvedValue:
    async def test_the_order_preview_quotes_the_symbol_s_own_bps(self) -> None:
        book = book_dict()
        book["slippage"] = {
            "model": "bps",
            "bps": "2",
            "est_spread_bps": "2",
            "per_instrument": {"BTC/USDT:USDT": {"bps": "0.4", "est_spread_bps": "0.02"}},
        }
        env = build_env(make_cfg(book))
        await env.tick("65000", bid="64999.9", ask="65000")
        s = env.broker.preview(env.req())["slippage"]
        assert s["bps"] == "0.4" and s["est_spread_bps"] == "0.02"
        assert s["est_fill"] == "65002.6"


def test_slippage_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        SlippageConfig.model_validate({"model": "bps", "nonsense": 1})
