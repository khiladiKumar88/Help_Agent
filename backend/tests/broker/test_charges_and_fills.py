"""Charges (worked examples, computed by hand) and fill-price models."""

from __future__ import annotations

from decimal import Decimal

import pytest

from papermind.broker.charges import CryptoCharges, IndiaFnoCharges, make_charges
from papermind.broker.fills import exit_trigger_price, market_fill, touch_price
from papermind.core.config import CryptoChargesConfig, IndiaFnoChargesConfig, Market, Segment, SlippageConfig
from papermind.core.types import Instrument, InstrumentKind, Liquidity, Side, Tick
from tests.conftest import BTC, T0, ccxt_instruments, crypto_charges, india_charges

D = Decimal


def nifty_option() -> Instrument:
    return Instrument(
        id="india:NFO:NIFTY26JAN25000CE",
        market=Market.INDIA,
        segment=Segment.OPTIONS,
        exchange="NFO",
        symbol="NIFTY26JAN25000CE",
        underlying="NIFTY",
        kind=InstrumentKind.CE,
        quote_ccy="INR",
        settle_ccy="INR",
        lot_size=D("75"),
        tick_size=D("0.05"),
        qty_step=D("75"),
        min_qty=D("75"),
        strike=D("25000"),
    )


def nifty_future() -> Instrument:
    return nifty_option().model_copy(update={"kind": InstrumentKind.FUT, "segment": Segment.FUTURES, "strike": None})


class TestCryptoCharges:
    def test_taker_and_maker(self) -> None:
        c = CryptoCharges(CryptoChargesConfig.model_validate(crypto_charges()))
        btc = ccxt_instruments()[0]
        taker = c.compute(btc, Side.BUY, D("0.01"), D("65000"), Liquidity.TAKER)
        assert taker.exchange_fee == D("0.325") and taker.total == D("0.325")  # 650 * 0.05%
        maker = c.compute(btc, Side.SELL, D("0.01"), D("65000"), Liquidity.MAKER)
        assert maker.total == D("0.13")  # 650 * 0.02%

    def test_gst_on_fees(self) -> None:
        c = CryptoCharges(CryptoChargesConfig.model_validate({**crypto_charges(), "gst_pct": "18"}))
        b = c.compute(ccxt_instruments()[0], Side.BUY, D("0.01"), D("65000"), Liquidity.TAKER)
        assert b.gst == D("0.0585") and b.total == D("0.3835")

    def test_contract_size_multiplies_notional(self) -> None:
        c = CryptoCharges(CryptoChargesConfig.model_validate(crypto_charges()))
        inst = ccxt_instruments()[0].model_copy(update={"contract_size": D("0.001")})
        assert c.compute(inst, Side.BUY, D("10"), D("65000"), Liquidity.TAKER).total == D("0.325")

    def test_round_trip_is_taker_both_legs(self) -> None:
        c = CryptoCharges(CryptoChargesConfig.model_validate(crypto_charges()))
        assert c.round_trip(ccxt_instruments()[0], Side.BUY, D("0.01"), D("65000"), D("64000")) == D("0.645")


class TestIndiaCharges:
    def cfg(self) -> IndiaFnoCharges:
        return IndiaFnoCharges(IndiaFnoChargesConfig.model_validate(india_charges()))

    def test_option_buy(self) -> None:
        # 75 x 100 = 7500 premium turnover
        b = self.cfg().compute(nifty_option(), Side.BUY, D("75"), D("100"), Liquidity.TAKER)
        assert b.brokerage == D("20.00")
        assert b.stt == D("0.00")  # STT only on sell side
        assert b.exchange_txn == D("2.63")  # 7500 * 0.03503% = 2.62725
        assert b.sebi == D("0.01")  # 7500 * 10 / 1e7 = 0.0075
        assert b.stamp == D("0.22")  # 7500 * 0.003% = 0.225 (banker's rounding)
        assert b.gst == D("4.07")  # 18% of (20 + 2.62725 + 0.0075) = 4.0742...
        assert b.total == D("26.93")

    def test_option_sell(self) -> None:
        b = self.cfg().compute(nifty_option(), Side.SELL, D("75"), D("120"), Liquidity.TAKER)
        assert b.stt == D("13.50")  # 9000 * 0.15%
        assert b.stamp == D("0.00")  # stamp only on buy side
        assert b.exchange_txn == D("3.15")
        assert b.gst == D("4.17")
        assert b.total == D("40.83")

    def test_future_rates(self) -> None:
        # 75 x 25000 = 18,75,000 turnover
        buy = self.cfg().compute(nifty_future(), Side.BUY, D("75"), D("25000"), Liquidity.TAKER)
        assert buy.exchange_txn == D("32.44")  # 0.00173% = 32.4375
        assert buy.stamp == D("37.50")  # 0.002%
        sell = self.cfg().compute(nifty_future(), Side.SELL, D("75"), D("25000"), Liquidity.TAKER)
        assert sell.stt == D("937.50")  # 0.05%

    def test_brokerage_pct_cap(self) -> None:
        c = IndiaFnoCharges(IndiaFnoChargesConfig.model_validate({**india_charges(), "brokerage_pct_cap": "0.25"}))
        b = c.compute(nifty_option(), Side.BUY, D("75"), D("2"), Liquidity.TAKER)  # turnover 150
        assert b.brokerage == D("0.38")  # min(20, 0.375)

    def test_round_trip_and_factory(self) -> None:
        c = make_charges(IndiaFnoChargesConfig.model_validate(india_charges()))
        assert isinstance(c, IndiaFnoCharges)
        assert c.round_trip(nifty_option(), Side.BUY, D("75"), D("100"), D("120")) == D("67.76")
        assert isinstance(make_charges(CryptoChargesConfig.model_validate(crypto_charges())), CryptoCharges)


class TestFills:
    def test_buy_at_ask_plus_bps_rounded_up(self) -> None:
        btc = ccxt_instruments()[0]
        t = Tick(instrument_id=BTC, ts=T0, ltp=D("65000"), bid=D("64999.9"), ask=D("65000"))
        q = market_fill(btc, Side.BUY, t, SlippageConfig(bps=D("2")))
        assert q.reference_kind == "ask" and q.reference_price == D("65000")
        assert q.price == D("65013")  # 65000 * 1.0002
        assert q.slippage == D("13")

    def test_sell_at_bid_minus_bps_rounded_down(self) -> None:
        btc = ccxt_instruments()[0]
        t = Tick(instrument_id=BTC, ts=T0, ltp=D("65000"), bid=D("64999.9"), ask=D("65000"))
        q = market_fill(btc, Side.SELL, t, SlippageConfig(bps=D("2")))
        assert q.reference_kind == "bid"
        assert q.price == D("64986.9")  # 64999.9 * 0.9998 = 64986.90002, floored to the 0.1 tick
        assert q.price <= D("64999.9") * D("0.9998")

    def test_no_quote_uses_ltp_half_spread(self) -> None:
        t = Tick(instrument_id=BTC, ts=T0, ltp=D("65000"))
        ref, kind = touch_price(t, Side.BUY, SlippageConfig(est_spread_bps=D("2")))
        assert kind == "ltp_half_spread" and ref == D("65006.5")
        ref, _ = touch_price(t, Side.SELL, SlippageConfig(est_spread_bps=D("2")))
        assert ref == D("64993.5")

    def test_spread_model_extra_ticks(self) -> None:
        opt = nifty_option()
        t = Tick(instrument_id=opt.id, ts=T0, ltp=D("100"), bid=D("99.9"), ask=D("100.1"))
        q = market_fill(opt, Side.BUY, t, SlippageConfig(model="spread", extra_ticks=2))
        assert q.price == D("100.2")
        q = market_fill(opt, Side.SELL, t, SlippageConfig(model="spread", extra_ticks=1))
        assert q.price == D("99.85")

    def test_price_never_non_positive(self) -> None:
        opt = nifty_option()
        t = Tick(instrument_id=opt.id, ts=T0, ltp=D("0.05"), bid=D("0.05"), ask=D("0.1"))
        q = market_fill(opt, Side.SELL, t, SlippageConfig(model="spread", extra_ticks=5))
        assert q.price == opt.tick_size

    @pytest.mark.parametrize(("side", "expected"), [(Side.SELL, D("99")), (Side.BUY, D("101"))])
    def test_exit_trigger_price(self, side: Side, expected: Decimal) -> None:
        t = Tick(instrument_id=BTC, ts=T0, ltp=D("100"), bid=D("99"), ask=D("101"))
        assert exit_trigger_price(t, side) == expected
        assert exit_trigger_price(Tick(instrument_id=BTC, ts=T0, ltp=D("100")), side) == D("100")
