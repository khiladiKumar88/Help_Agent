# PaperMind

A self-improving AI **paper-trading** agent with a web UI. It trades **simulated money only**,
on Indian markets (Nifty/BankNifty futures & options) and crypto (spot, perpetuals, BTC/ETH
options). It learns from its own trades using a "second brain" and statistical learning.
Risk rules are plain code that the AI can never override.

> ⚠️ PaperMind never places real orders. It uses public market-data endpoints only, and a
> test fails the build if any order-placement API is referenced.

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+.

**Windows / PowerShell** (no `make` needed — folder names with spaces are fine):

```powershell
Copy-Item .env.example .env   # nothing required for Phase 1
.\run.ps1 install
.\run.ps1 dev                 # live crypto prices (Binance public data via ccxt)
# or
.\run.ps1 dev-sim             # offline: seeded simulated prices (labelled SIMULATED in the UI)
```

`dev` and `dev-sim` run the backend and the frontend together in one console; Ctrl+C stops both.
`.\run.ps1 help` lists every command, and `-DryRun` prints what a command would run without
running it. If PowerShell refuses to run the script, allow local scripts for that session with
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

**macOS / Linux** (the same commands via `make`):

```bash
cp .env.example .env
make install
make dev                  # or: make dev-sim
```

Open http://127.0.0.1:5173. API docs are at http://127.0.0.1:8000/docs.

Without either runner:

```bash
cd backend && uv sync && uv run uvicorn papermind.main:app --port 8000
cd frontend && npm install && npm run dev
```

## Using it (Phase 1)

1. Top bar: **Crypto · Futures · Intraday · Manual**.
2. In **Manual paper order**, pick BTC/USDT perp. Enter a qty and a **stop-loss** (required),
   plus an optional target and leverage. The panel runs a live risk check. It shows the
   estimated entry, fees, margin, risk if the stop is hit and reward:risk, or the exact
   rule that blocks the order. "max by risk" fills in the largest size your risk limit allows.
   It also shows the **slippage the model assumes** (e.g. "2 bps worse than the ask") and the
   fill that implies, so the difference between the estimate and your actual fill is explained
   rather than surprising. Slippage is set per book under `slippage:` in the book YAML.
3. Place the order. The ticket clears, and you get **"Paper order accepted — fills on the next
   tick"**, which becomes **"Position opened"** with the real fill price, SL and target once the
   fill lands. **Open positions** then shows live P&L **after fees** (including estimated exit
   fees) and the R-multiple. Entry/SL/target lines appear on the chart, and the chart widens its
   price scale to keep them visible; a level too far away to fit without flattening the candles
   is listed under the chart as "Off the chart: SL 62,000.0 below" instead.
4. **Close**, or **Edit** the stop/target. Loosening a stop is re-checked against your max
   risk per trade. Stop-loss and target work as one-cancels-the-other.
5. The **Kill switch** closes every paper position at market and halts all books until you
   release it.

## Backtests and replays (Phase 2)

Open **Backtest / Replay**:

1. **Market data**: pick a source (the live exchange, or `simulated` for offline testing), symbol
   and bar size, then **Download / update**. Data is cached in `history.db`; later downloads
   only fetch what's new. The card shows the stored range and any gaps.
2. **Backtest**: one strategy, default parameters, over a date range.
3. **Walk-forward**: parameters are tuned on each training window (small grid) and judged
   on the next unseen test window. **Only the test windows are reported.**
4. **Replay a day**: runs every strategy enabled for the book through the full pipeline
   (scanner → agent → risk → paper broker) on a simulated clock, with a chart of every entry
   and exit.

Every result shows net profit after fees, trades, win rate, average R, profit factor and max
drawdown, next to the **random baseline** (random entries, same risk and frequency) and
**buy-and-hold**. It also says in plain English whether the strategy beat the baseline, and
whether the trade count is high enough to mean anything (30 or more).

Strategies (configure per book under `strategies:` in the book YAML): Opening Range Breakout,
VWAP reclaim/rejection, EMA 9/21 pullback with ADX filter, Supertrend flip, RSI mean-reversion
(ranging regime only), funding-rate fade (crypto perps). In **Manual** mode, live signals are
shown on Home as analysis only.

Default crypto book: 1,000 USDT, 1% risk per trade (fees included), max 3x leverage,
2 open positions, 10 trades/day, and a 3% daily loss limit. Hitting the limit halts the
book until 00:00 UTC and flattens its positions. Edit `config/books/crypto-futures-intraday.yaml`.

## Configuration

| File | What |
|---|---|
| `config/default.yaml` | DB URL, stale-data threshold, crypto exchange/symbols, simulated feed |
| `config/books/*.yaml` | One file per book: currency, capital, risk limits, slippage, session |
| `config/charges/*.yaml` | Fee schedules with `source_url`, `as_of`, `verified` — **verify before trusting** |
| `config/calendars/*.yaml` | Exchange holidays (India, Phase 5) |
| `backend/migrations/` | Alembic migrations (run automatically on startup; your Phase-1 DB is upgraded in place) |
| `.env` | Secrets only (Gemini, Angel One). Never committed, never logged. |

## Tests

```powershell
.\run.ps1 check    # Windows
```

```bash
make check         # macOS / Linux
```

Either one runs lint + types + every test, with the backend coverage report. `run.ps1 test`
(or `make test`) runs just the tests.

## Roadmap

1. **Foundation** ✅: data, risk, paper broker, journal, UI shell with manual trading
2. **Strategies + backtesting** ✅: history store, 6 strategies, regime, scanner, baseline, backtest, walk-forward, replay
3. LLM analyst (Gemini), Co-pilot and Auto modes, signal cards
4. Second brain (library, episodes, playbook), reflection, lesson promotion, bandit
5. Indian markets via Angel One SmartAPI (market data only)
6. Hardening: crypto options, news sentiment, reconnects, Docker, backups, E2E
