"""Built-in strategies. Each uses only the closed candles in ctx.frame (no lookahead)."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from papermind.core.config import Market
from papermind.core.types import Direction
from papermind.regime.detector import RegimeLabel
from papermind.strategies.base import ParamValue, SignalIntent, Strategy, StrategyContext


def _nan(*xs: float) -> bool:
    return any(math.isnan(x) for x in xs)


class OpeningRangeBreakout(Strategy):
    id = "orb"
    name = "Opening Range Breakout"
    description = (
        "Marks the high/low of the first N minutes after the session opens; a close beyond the range "
        "(plus a small ATR buffer) enters in that direction with the stop at the other side of the range. "
        "At most one signal per instrument per day. Crypto uses the 00:00 UTC day open."
    )
    default_params = {"range_minutes": 30, "rr": 2.0, "buffer_atr": 0.1}
    param_grid = {"range_minutes": [15, 30], "rr": [1.5, 2.0], "buffer_atr": [0.0, 0.2]}

    def __init__(self, params: dict[str, ParamValue] | None = None) -> None:
        super().__init__(params)
        self._fired: dict[str, date] = {}

    def reset(self) -> None:
        self._fired.clear()

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        session = ctx.book.session
        tz = ZoneInfo(session.timezone if session else "UTC")
        open_t = (session.market_open or session.entry_start) if session else time(0, 0)
        local_now = ctx.now.astimezone(tz)
        today = local_now.date()
        if self._fired.get(ctx.instrument.id) == today:
            return None
        day_open = datetime.combine(today, open_t, tz)
        range_end = day_open + timedelta(minutes=self.pi("range_minutes"))
        if ctx.now <= range_end:
            return None
        f = ctx.frame
        idx = [i for i, ts in enumerate(f.ts) if day_open <= ts.astimezone(tz) < range_end]
        if not idx:
            return None
        hi = float(max(f.high[i] for i in idx))
        lo = float(min(f.low[i] for i in idx))
        close, prev_close, a = f.last(f.close), f.last(f.close, 1), f.last(f.atr(14))
        if _nan(a):
            return None
        buf = self.p("buffer_atr") * a
        if close > hi + buf and prev_close <= hi + buf:
            sig = self.build(
                ctx,
                Direction.LONG,
                lo,
                self.p("rr"),
                f"Close {close:.2f} broke above the {self.pi('range_minutes')}-min opening range "
                f"high {hi:.2f}; stop at range low {lo:.2f}.",
                range_high=hi,
                range_low=lo,
            )
        elif close < lo - buf and prev_close >= lo - buf:
            sig = self.build(
                ctx,
                Direction.SHORT,
                hi,
                self.p("rr"),
                f"Close {close:.2f} broke below the {self.pi('range_minutes')}-min opening range "
                f"low {lo:.2f}; stop at range high {hi:.2f}.",
                range_high=hi,
                range_low=lo,
            )
        else:
            return None
        if sig is not None:
            self._fired[ctx.instrument.id] = today
        return sig


class VwapReclaim(Strategy):
    id = "vwap_reclaim"
    name = "VWAP reclaim / rejection"
    description = (
        "Long when price closes back above the session VWAP after closing below it, with the trend EMA "
        "rising; short on the mirror-image rejection. Stop beyond the recent swing or an ATR multiple."
    )
    default_params = {"rr": 2.0, "atr_stop": 1.0, "trend_ema": 50}
    param_grid = {"rr": [1.5, 2.0], "atr_stop": [1.0, 1.5]}

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        f = ctx.frame
        v, e = f.vwap(), f.ema(self.pi("trend_ema"))
        c, c1 = f.last(f.close), f.last(f.close, 1)
        v0, v1 = f.last(v), f.last(v, 1)
        e0, e5 = f.last(e), f.last(e, 5)
        a = f.last(f.atr(14))
        if _nan(v0, v1, e0, e5, a):
            return None
        if f.session_ids()[-1] != f.session_ids()[-2]:
            return None  # first bar of a session: VWAP just reset
        if c1 < v1 and c > v0 and e0 > e5:
            stop = min(float(f.low[-3:].min()), c - self.p("atr_stop") * a)
            return self.build(
                ctx,
                Direction.LONG,
                stop,
                self.p("rr"),
                f"Price reclaimed VWAP ({v0:.2f}) with the {self.pi('trend_ema')}-EMA rising.",
                vwap=v0,
            )
        if c1 > v1 and c < v0 and e0 < e5:
            stop = max(float(f.high[-3:].max()), c + self.p("atr_stop") * a)
            return self.build(
                ctx,
                Direction.SHORT,
                stop,
                self.p("rr"),
                f"Price was rejected at VWAP ({v0:.2f}) with the {self.pi('trend_ema')}-EMA falling.",
                vwap=v0,
            )
        return None


class EmaTrendPullback(Strategy):
    id = "ema_pullback"
    name = "EMA 9/21 trend pullback (ADX filter)"
    description = (
        "In a trend (EMA9 above EMA21 and ADX above a threshold, +DI leading), buy a pullback that touches "
        "the EMA9 and closes back above it; mirror for shorts. Stop just beyond the EMA21 / bar extreme."
    )
    default_params = {"adx_min": 20, "rr": 2.0}
    param_grid = {"adx_min": [20, 25], "rr": [1.5, 2.0]}

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        f = ctx.frame
        e9, e21 = f.ema(9), f.ema(21)
        adx, pdi, mdi = f.adx(14)
        a = f.last(f.atr(14))
        vals = (f.last(e9), f.last(e21), f.last(adx), f.last(pdi), f.last(mdi), a, f.last(e21, 1))
        if _nan(*vals):
            return None
        ema9, ema21, adx_v, p, m, _, ema21_prev = vals
        o, h, lo, c = f.last(f.open), f.last(f.high), f.last(f.low), f.last(f.close)
        c_prev = f.last(f.close, 1)
        if adx_v < self.p("adx_min"):
            return None
        if ema9 > ema21 and p > m and lo <= ema9 and c > ema9 and c > o and c_prev > ema21_prev:
            return self.build(
                ctx,
                Direction.LONG,
                min(lo, ema21) - 0.25 * a,
                self.p("rr"),
                f"Up-trend (ADX {adx_v:.0f}); pullback to EMA9 {ema9:.2f} bounced.",
                adx=adx_v,
            )
        if ema9 < ema21 and m > p and h >= ema9 and c < ema9 and c < o and c_prev < ema21_prev:
            return self.build(
                ctx,
                Direction.SHORT,
                max(h, ema21) + 0.25 * a,
                self.p("rr"),
                f"Down-trend (ADX {adx_v:.0f}); pullback to EMA9 {ema9:.2f} rejected.",
                adx=adx_v,
            )
        return None


class SupertrendFlip(Strategy):
    id = "supertrend_flip"
    name = "Supertrend flip"
    description = "Enters when the Supertrend direction flips on a closed bar; the stop is the new Supertrend line."
    default_params = {"period": 10, "mult": 3.0, "rr": 2.0}
    param_grid = {"mult": [2.0, 3.0], "rr": [1.5, 2.5]}

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        f = ctx.frame
        line, d = f.supertrend(self.pi("period"), self.p("mult"))
        a = f.last(f.atr(14))
        if len(d) < 2 or d[-1] == 0 or d[-2] == 0 or d[-1] == d[-2] or _nan(f.last(line), a):
            return None
        stop = f.last(line)
        c = f.last(f.close)
        if abs(c - stop) < 0.2 * a:
            return None
        direction = Direction.LONG if d[-1] > 0 else Direction.SHORT
        word = "up" if d[-1] > 0 else "down"
        return self.build(
            ctx,
            direction,
            stop,
            self.p("rr"),
            f"Supertrend({self.pi('period')},{self.p('mult'):g}) flipped {word}; stop at the line {stop:.2f}.",
        )


class RsiMeanReversion(Strategy):
    id = "rsi_mean_reversion"
    name = "RSI mean-reversion (ranging regime only)"
    description = (
        "Only when the regime is 'ranging': buy when RSI crosses back above the oversold level, sell when it "
        "crosses back below overbought. Target the 20-bar mean; skipped if the target is too close."
    )
    default_params = {"rsi_len": 14, "lower": 30, "stop_atr": 1.5}
    param_grid = {"lower": [25, 30], "stop_atr": [1.5, 2.0]}

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        if ctx.regime.label is not RegimeLabel.RANGING:
            return None
        f = ctx.frame
        r = f.rsi(self.pi("rsi_len"))
        r0, r1, a, mean = f.last(r), f.last(r, 1), f.last(f.atr(14)), f.last(f.sma(20))
        c = f.last(f.close)
        if _nan(r0, r1, a, mean):
            return None
        lower, upper = self.p("lower"), 100 - self.p("lower")
        stop_dist = self.p("stop_atr") * a
        if r1 < lower <= r0 and mean - c >= 0.8 * stop_dist:
            return self.build(
                ctx,
                Direction.LONG,
                c - stop_dist,
                None,
                f"Ranging market; RSI crossed back above {lower:g} ({r0:.0f}). Target the mean {mean:.2f}.",
                target=mean,
                rsi=r0,
            )
        if r1 > upper >= r0 and c - mean >= 0.8 * stop_dist:
            return self.build(
                ctx,
                Direction.SHORT,
                c + stop_dist,
                None,
                f"Ranging market; RSI crossed back below {upper:g} ({r0:.0f}). Target the mean {mean:.2f}.",
                target=mean,
                rsi=r0,
            )
        return None


class FundingFade(Strategy):
    id = "funding_fade"
    name = "Funding-rate extreme fade (crypto perps)"
    description = (
        "When perpetual funding is extreme (crowded longs or shorts) and price is stretched from its 20-EMA "
        "with RSI confirming, fade the crowd. Skipped in strong trends (ADX > 35) in the crowd's direction."
    )
    markets = frozenset({Market.CRYPTO})
    default_params = {"threshold": 0.0003, "rr": 2.0}
    param_grid = {"threshold": [0.0003, 0.0005], "rr": [1.5, 2.0]}

    def evaluate(self, ctx: StrategyContext) -> SignalIntent | None:
        if ctx.funding_rate is None or not ctx.instrument.is_perp:
            return None
        fr = float(ctx.funding_rate)
        f = ctx.frame
        e20, a, r = f.last(f.ema(20)), f.last(f.atr(14)), f.last(f.rsi(14))
        adx, pdi, mdi = f.adx(14)
        adx_v, p, m = f.last(adx), f.last(pdi), f.last(mdi)
        c = f.last(f.close)
        if _nan(e20, a, r, adx_v, p, m):
            return None
        th = self.p("threshold")
        if fr >= th and c > e20 + a and r >= 60 and not (adx_v > 35 and p > m):
            stop = float(f.high[-3:].max()) + 0.5 * a
            return self.build(
                ctx,
                Direction.SHORT,
                stop,
                self.p("rr"),
                f"Funding {fr * 100:.3f}% (crowded longs) with price stretched above EMA20; fading.",
                funding=fr,
            )
        if fr <= -th and c < e20 - a and r <= 40 and not (adx_v > 35 and m > p):
            stop = float(f.low[-3:].min()) - 0.5 * a
            return self.build(
                ctx,
                Direction.LONG,
                stop,
                self.p("rr"),
                f"Funding {fr * 100:.3f}% (crowded shorts) with price stretched below EMA20; fading.",
                funding=fr,
            )
        return None


STRATEGIES: dict[str, type[Strategy]] = {
    s.id: s
    for s in (OpeningRangeBreakout, VwapReclaim, EmaTrendPullback, SupertrendFlip, RsiMeanReversion, FundingFade)
}


def make_strategy(strategy_id: str, params: dict[str, ParamValue] | None = None) -> Strategy:
    try:
        cls = STRATEGIES[strategy_id]
    except KeyError as exc:
        raise KeyError(f"unknown strategy '{strategy_id}' (have: {sorted(STRATEGIES)})") from exc
    return cls(params)
