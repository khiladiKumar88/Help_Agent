"""History store + downloader: pagination, incremental sync, gaps, retries, funding, simulated source."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import ccxt
import pytest

from papermind.core.clock import ReplayClock
from papermind.data.history import (
    HistoryDownloader,
    HistoryStore,
    SimulatedHistoryExchange,
    expected_bars,
    from_ms,
    funding_rate_at,
    to_ms,
)
from tests.conftest import load_fixture

SYM = "BTC/USDT:USDT"
START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


class PagingExchange:
    """Serves fixture rows with a hard page limit, like a real exchange."""

    precisionMode = 4
    apiKey = ""
    secret = ""

    def __init__(self, clock: ReplayClock, rows: list[list[Any]] | None = None, fail_first: int = 0) -> None:
        self.clock = clock
        self.rows = rows if rows is not None else load_fixture("binanceusdm_btc_ohlcv_1m.json")
        self.markets = load_fixture("binanceusdm_markets.json")
        self.has = {"fetchFundingRateHistory": True}
        self.calls = 0
        self.fail_first = fail_first
        self.closed = False
        self.funding = [
            {"timestamp": to_ms(START) + i * 8 * 3600_000, "fundingRate": 0.0001 * (i + 1)} for i in range(3)
        ]

    async def load_markets(self, reload: bool = False) -> dict[str, Any]:
        return self.markets

    async def fetch_ohlcv(self, symbol: str, tf: str, since: int | None, limit: int) -> list[list[Any]]:
        self.calls += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ccxt.NetworkError("temporary")
        now = to_ms(self.clock.now())
        out = [r for r in self.rows if (since is None or r[0] >= since) and r[0] <= now]
        return out[: min(limit, 50)]  # exchange caps pages at 50

    async def fetch_funding_rate_history(self, symbol: str, since: int | None, limit: int) -> list[dict[str, Any]]:
        return [f for f in self.funding if since is None or f["timestamp"] >= since][:limit]

    async def close(self) -> None:
        self.closed = True


def make(tmp_path: Path, clock: ReplayClock, ex: Any, page: int = 1000) -> tuple[HistoryStore, HistoryDownloader]:
    store = HistoryStore(f"sqlite:///{tmp_path / 'h.db'}")

    async def no_sleep(_: float) -> None:
        return None

    return store, HistoryDownloader(store, lambda _id: ex, clock, page_limit=page, sleep=no_sleep)


async def test_paginated_download_skips_forming_bar(tmp_path: Path) -> None:
    clock = ReplayClock(START + timedelta(minutes=119, seconds=30))  # last fixture bar (09:119) still forming
    ex = PagingExchange(clock)
    store, dl = make(tmp_path, clock, ex)
    progress: list[float] = []
    rep = await dl.sync("binanceusdm", SYM, "1m", START, progress=lambda p, _m: progress.append(p))
    assert rep.bars_written == 119 and ex.calls >= 3  # 50-bar pages
    cov = store.coverage("binanceusdm", SYM, "1m")
    assert cov.bars == 119 and cov.missing_bars == 0 and cov.gaps == [] and cov.complete_pct == 100.0
    assert cov.last == START + timedelta(minutes=118)
    assert progress[-1] == 1.0 and ex.closed
    assert rep.funding_written == 1  # only the funding stamp that is not in the future


async def test_incremental_sync_only_fetches_new_bars(tmp_path: Path) -> None:
    clock = ReplayClock(START + timedelta(minutes=60))
    ex = PagingExchange(clock)
    store, dl = make(tmp_path, clock, ex)
    await dl.sync("binanceusdm", SYM, "1m", START)
    assert store.bounds("binanceusdm", SYM, "1m")[2] == 60  # type: ignore[index]
    ex.calls = 0
    clock.advance(minutes=30)
    rep = await dl.sync("binanceusdm", SYM, "1m", START)
    assert rep.bars_written == 30 and ex.calls == 1
    ex.calls = 0
    rep = await dl.sync("binanceusdm", SYM, "1m", START)  # nothing new
    assert rep.bars_written == 0 and ex.calls == 0


async def test_earlier_start_prepends(tmp_path: Path) -> None:
    clock = ReplayClock(START + timedelta(minutes=120))
    ex = PagingExchange(clock)
    store, dl = make(tmp_path, clock, ex)
    await dl.sync("binanceusdm", SYM, "1m", START + timedelta(minutes=60))
    assert store.bounds("binanceusdm", SYM, "1m")[0] == to_ms(START + timedelta(minutes=60))  # type: ignore[index]
    await dl.sync("binanceusdm", SYM, "1m", START)
    assert store.bounds("binanceusdm", SYM, "1m")[2] == 120  # type: ignore[index]


async def test_gaps_are_reported_and_repairable(tmp_path: Path) -> None:
    clock = ReplayClock(START + timedelta(minutes=120))
    rows = load_fixture("binanceusdm_btc_ohlcv_1m.json")
    holey = [r for i, r in enumerate(rows) if not (10 <= i < 15) and i != 40]
    ex = PagingExchange(clock, holey)
    store, dl = make(tmp_path, clock, ex)
    await dl.sync("binanceusdm", SYM, "1m", START)
    cov = store.coverage("binanceusdm", SYM, "1m")
    assert cov.missing_bars == 6 and len(cov.gaps) == 2
    assert cov.gaps[0].start == START + timedelta(minutes=10) and cov.gaps[0].missing_bars == 5
    ex.rows = rows  # exchange now has the data
    await dl.sync("binanceusdm", SYM, "1m", START, repair_gaps=True)
    assert store.coverage("binanceusdm", SYM, "1m").missing_bars == 0


async def test_retries_network_errors(tmp_path: Path) -> None:
    clock = ReplayClock(START + timedelta(minutes=30))
    ex = PagingExchange(clock, fail_first=2)
    _store, dl = make(tmp_path, clock, ex)
    rep = await dl.sync("binanceusdm", SYM, "1m", START)
    assert rep.bars_written == 30
    (tmp_path / "x").mkdir()
    _, dl2 = make(tmp_path / "x", clock, PagingExchange(clock, fail_first=99))
    with pytest.raises(ccxt.NetworkError):
        await dl2.sync("binanceusdm", SYM, "1m", START)


async def test_unknown_symbol(tmp_path: Path) -> None:
    clock = ReplayClock(START)
    _, dl = make(tmp_path, clock, PagingExchange(clock))
    with pytest.raises(ValueError, match="not listed"):
        await dl.sync("binanceusdm", "DOGE/USDT:USDT", "1m", START)


def test_load_candles_range_and_datasets(tmp_path: Path) -> None:
    store = HistoryStore(f"sqlite:///{tmp_path / 'h.db'}")
    rows = load_fixture("binanceusdm_btc_ohlcv_1m.json")[:10]
    store.upsert_candles("binanceusdm", SYM, "1m", rows)
    store.upsert_candles("binanceusdm", SYM, "1m", rows[:2])  # idempotent upsert
    got = store.load_candles("binanceusdm", SYM, "1m", START + timedelta(minutes=2), START + timedelta(minutes=5), "i")
    assert [c.ts_open for c in got] == [START + timedelta(minutes=m) for m in (2, 3, 4)]
    assert isinstance(got[0].close, Decimal) and got[0].instrument_id == "i"
    ds = store.datasets()
    assert ds[0]["bars"] == 10 and ds[0]["timeframe"] == "1m"
    assert store.coverage("x", "y", "1m").bars == 0
    assert store.upsert_candles("x", "y", "1m", []) == 0 and store.upsert_funding("x", "y", []) == 0
    store.upsert_funding("binanceusdm", SYM, [(to_ms(START), "0.0001"), (to_ms(START) + 8 * 3600_000, "-0.0002")])
    pts = store.load_funding("binanceusdm", SYM, START, START + timedelta(days=1))
    assert funding_rate_at(pts, START + timedelta(hours=7)) == Decimal("0.0001")
    assert funding_rate_at(pts, START + timedelta(hours=8)) == Decimal("-0.0002")
    assert funding_rate_at(pts, START - timedelta(hours=1)) is None
    assert expected_bars(START, START + timedelta(hours=1), "5m") == 12
    assert from_ms(to_ms(START)) == START


async def test_simulated_history_is_deterministic_and_pagination_safe(tmp_path: Path) -> None:
    clock = ReplayClock(datetime(2026, 1, 3, tzinfo=UTC))
    a = SimulatedHistoryExchange(clock)
    b = SimulatedHistoryExchange(clock)
    since = to_ms(datetime(2026, 1, 1, tzinfo=UTC))
    big = await a.fetch_ohlcv(SYM, "5m", since, 100)
    p1 = await b.fetch_ohlcv(SYM, "5m", since, 40)
    p2 = await b.fetch_ohlcv(SYM, "5m", p1[-1][0] + 300_000, 60)
    assert big == p1 + p2
    for prev, cur in itertools.pairwise(big):
        assert prev[4] == cur[1]  # continuous: next open == previous close
        assert Decimal(cur[2]) >= max(Decimal(cur[1]), Decimal(cur[4]))
        assert Decimal(cur[3]) <= min(Decimal(cur[1]), Decimal(cur[4]))
    one_h = await a.fetch_ohlcv(SYM, "1h", since, 2)
    assert one_h[0][1] == big[0][1] and one_h[0][4] == big[11][4]
    fund = await a.fetch_funding_rate_history(SYM, since, 5)
    assert len(fund) == 5 and fund[1]["timestamp"] - fund[0]["timestamp"] == 8 * 3600_000
    assert (await a.load_markets())[SYM]["swap"] is True
    future = await a.fetch_ohlcv(SYM, "5m", to_ms(clock.now()), 10)
    assert future == []  # never produces bars beyond "now"


async def test_simulated_download_end_to_end(tmp_path: Path) -> None:
    clock = ReplayClock(datetime(2026, 1, 2, tzinfo=UTC))
    sim = SimulatedHistoryExchange(clock)
    store, dl = make(tmp_path, clock, sim)
    rep = await dl.sync("simulated", SYM, "15m", datetime(2026, 1, 1, tzinfo=UTC))
    assert rep.bars_written == 96 and rep.funding_written == 3
    assert store.coverage("simulated", SYM, "15m").complete_pct == 100.0
