"""Technical indicators on numpy float arrays (oldest -> newest).

Conventions: values that are not yet defined (warm-up) are NaN. Wilder smoothing (RMA) for
RSI / ATR / ADX, seeded with a simple mean — the classic definitions. Indicators are for
signal logic only; all money maths stays in Decimal elsewhere.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

Arr = npt.NDArray[np.float64]


def sma(x: Arr, n: int) -> Arr:
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


_POW_CACHE: dict[tuple[float, int], tuple[Arr, Arr, Arr]] = {}


def _powers(r: float, block: int) -> tuple[Arr, Arr, Arr]:
    key = (r, block)
    if key not in _POW_CACHE:
        k = np.arange(block, dtype=np.float64)
        _POW_CACHE[key] = (r ** (k + 1), r ** (-k), r**k)
    return _POW_CACHE[key]


def _recursive(x: Arr, r: float, a: float, y0: float) -> Arr:
    """y[i] = r * y[i-1] + a * x[i] with y[-1] = y0, vectorized in blocks (exact up to float rounding).

    Block length is chosen so r**-k stays below 1e12, so the closed form never loses precision.
    """
    out = np.empty(len(x))
    if len(x) == 0:
        return out
    if r < 0.5:  # tiny windows: plain loop is cheap and stable
        y = y0
        for i, v in enumerate(x):
            y = r * y + a * v
            out[i] = y
        return out
    block = max(8, min(512, int(27.6 / -np.log(r))))  # r**-block <= ~1e12
    rp, rinv, rj = _powers(r, block)
    y = y0
    for s0 in range(0, len(x), block):
        xb = x[s0 : s0 + block]
        n = len(xb)
        yb = rp[:n] * y + a * rj[:n] * np.cumsum(xb * rinv[:n])
        out[s0 : s0 + n] = yb
        y = float(yb[-1])
    return out


def ema(x: Arr, n: int) -> Arr:
    """EMA with alpha = 2/(n+1), seeded with the SMA of the first n values."""
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    alpha = 2.0 / (n + 1)
    seed = float(np.mean(x[:n]))
    out[n - 1] = seed
    out[n:] = _recursive(x[n:], 1 - alpha, alpha, seed)
    return out


def rma(x: Arr, n: int, start: int = 0) -> Arr:
    """Wilder's moving average, seeded with the mean of x[start:start+n]."""
    out = np.full(len(x), np.nan)
    if len(x) < start + n:
        return out
    seed = float(np.mean(x[start : start + n]))
    out[start + n - 1] = seed
    out[start + n :] = _recursive(x[start + n :], (n - 1) / n, 1.0 / n, seed)
    return out


def rsi(close: Arr, n: int = 14) -> Arr:
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    diff = np.diff(close)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    ag = rma(gain, n)
    al = rma(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        r = np.where(al == 0, np.where(ag == 0, 50.0, 100.0), 100.0 - 100.0 / (1.0 + rs))
    out[1:] = np.where(np.isnan(ag), np.nan, r)
    return out


def true_range(high: Arr, low: Arr, close: Arr) -> Arr:
    prev = np.insert(close[:-1], 0, np.nan)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return tr


def atr(high: Arr, low: Arr, close: Arr, n: int = 14) -> Arr:
    return rma(true_range(high, low, close), n)


def adx(high: Arr, low: Arr, close: Arr, n: int = 14) -> tuple[Arr, Arr, Arr]:
    """Returns (ADX, +DI, -DI)."""
    size = len(close)
    nan = np.full(size, np.nan)
    if size < 2 * n + 1:
        return nan, nan.copy(), nan.copy()
    up = np.diff(high, prepend=np.nan)
    down = -np.diff(low, prepend=np.nan)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm[0] = minus_dm[0] = 0.0
    tr = true_range(high, low, close)
    tr_s = rma(tr, n, start=1)
    pdm_s = rma(plus_dm, n, start=1)
    mdm_s = rma(minus_dm, n, start=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * pdm_s / tr_s
        mdi = 100.0 * mdm_s / tr_s
        dx = 100.0 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.where((pdi + mdi) == 0, 0.0, dx)
    first = n  # first defined dx index
    adx_arr = rma(np.nan_to_num(dx, nan=0.0), n, start=first)
    adx_arr[: first + n - 1] = np.nan
    return adx_arr, pdi, mdi


def supertrend(high: Arr, low: Arr, close: Arr, n: int = 10, mult: float = 3.0) -> tuple[Arr, Arr]:
    """Returns (line, direction) where direction is +1 (up-trend, line below price) or -1."""
    size = len(close)
    line = np.full(size, np.nan)
    direction = np.zeros(size)
    a = atr(high, low, close, n)
    hl2 = (high + low) / 2.0
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    fu = np.full(size, np.nan)
    fl = np.full(size, np.nan)
    for i in range(size):
        if np.isnan(a[i]):
            continue
        if i == 0 or np.isnan(fu[i - 1]):
            fu[i], fl[i] = upper[i], lower[i]
            direction[i] = 1.0 if close[i] >= hl2[i] else -1.0
        else:
            fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
            if direction[i - 1] < 0:
                direction[i] = 1.0 if close[i] > fu[i] else -1.0
            else:
                direction[i] = -1.0 if close[i] < fl[i] else 1.0
        line[i] = fl[i] if direction[i] > 0 else fu[i]
    return line, direction


def vwap(high: Arr, low: Arr, close: Arr, volume: Arr, session_id: npt.NDArray[np.int64]) -> Arr:
    """Session-anchored VWAP; resets whenever session_id changes."""
    tp = (high + low + close) / 3.0
    if len(close) == 0:
        return np.full(0, np.nan)
    pv = np.cumsum(tp * volume)
    vol = np.cumsum(volume)
    new = np.concatenate([[True], session_id[1:] != session_id[:-1]])
    start = np.maximum.accumulate(np.where(new, np.arange(len(close)), 0))
    base_pv = np.where(start > 0, pv[start - 1], 0.0)
    base_v = np.where(start > 0, vol[start - 1], 0.0)
    sv = vol - base_v
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(sv > 0, (pv - base_pv) / sv, tp)
    return out


def realized_vol(close: Arr, n: int, periods_per_year: float) -> float:
    """Annualized stdev of log returns over the last n bars (NaN if not enough data)."""
    if len(close) < n + 1:
        return float("nan")
    r = np.diff(np.log(close[-(n + 1) :]))
    return float(np.std(r, ddof=1) * np.sqrt(periods_per_year))


def percentile_rank(x: Arr, lookback: int) -> float:
    """Fraction of the last `lookback` finite values that are <= the latest value (0..1)."""
    w = x[-lookback:]
    w = w[~np.isnan(w)]
    if len(w) < 2 or np.isnan(x[-1]):
        return float("nan")
    return float(np.mean(w <= x[-1]))


def pct_change(x: Arr, n: int) -> float:
    if len(x) <= n or np.isnan(x[-1]) or np.isnan(x[-1 - n]) or x[-1 - n] == 0:
        return float("nan")
    return float((x[-1] - x[-1 - n]) / x[-1 - n] * 100.0)
