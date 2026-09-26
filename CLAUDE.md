# PaperMind — guide for Claude / contributors

PaperMind is a **paper-only**, self-improving AI trading agent with a web UI, for Indian
markets (Nifty/BankNifty futures + options) and crypto (spot, perps, BTC/ETH options). It
trades simulated money only. It is built in phases; see "Status" below.

## Non-negotiable rules (never violate, never weaken)

1. **Paper only.** Never implement or call a real order-placement / cancellation / account
   / fund-movement endpoint (Angel One order APIs, exchange private APIs). Market data only.
   - `tests/test_no_order_placement.py` statically scans `backend/papermind/` and fails
     on any such call or on credentials passed to an exchange client.
   - `data/public_only.py` wraps every ccxt instance with an allow-list of public calls and
     refuses credentials at runtime.
2. **The LLM never controls risk.** Risk rules live in `risk/` as pure functions. Nothing
   the LLM outputs can change, override or bypass them. `require_stop_loss` can't be disabled.
3. **Fail safe.** Stale data (> `data.stale_after_seconds`), LLM error/rate-limit, invalid
   JSON, any exception ⇒ **NO TRADE**. Log it and keep running. Event-bus handlers
   are isolated so one failure never stops the engine. The UI shows disconnected/stale states.
4. **Everything is logged:** risk decisions (`risk_decisions`), orders, fills, trades,
   ledger, audit log (`audit_log`). LLM calls, signals and lessons get logged too once those
   phases land.
5. **Secrets only in `.env`** (`.env.example` is committed). Logging passes through
   `core/logging.RedactingFilter`; the API exposes secret *presence* only.
6. **No lookahead.** Everything reads time from the injected `Clock`. Use `ReplayClock` for
   replays and tests. Strategies/backtests only ever see **closed** candles (`CandleBuilder`
   emits closed bars only; `ohlcv_to_candles` drops the forming bar).

Also: never hardcode lot sizes, tick sizes, expiries or fee rates. They come from the
instrument master (ccxt markets / Angel One scrip master) or `config/`. Fee files carry
`source_url`, `as_of` and `verified`. Only a human flips `verified: true`.

## Architecture

```
config/                 YAML: default.yaml, books/*.yaml, charges/*.yaml, calendars/*.yaml
backend/papermind/
  core/        config (pydantic-settings + YAML), clock (Real/Replay), events (async bus),
               logging (JSON + redaction), money (Decimal helpers), types, serde
  db/          SQLAlchemy 2 models; SQLite WAL (Postgres-swappable: DecimalText -> NUMERIC)
  instruments/ registry: ccxt market -> Instrument (persisted, refreshed daily)
  data/        MarketDataProvider ABC; CcxtProvider (ws->poll fallback, public only);
               SimulatedProvider (seeded offline feed); MarketHub; CandleBuilder; watchdog
  risk/        rules.py (R000-R014, pure), manager.py (fixed chain + sizing), session.py
  broker/      PaperBroker (fills, slippage, OCO SL/target, trailing, funding, square-off,
               expiry, daily-loss halt, kill switch, crash recovery); charges; fills
  journal/     Journal: the only DB writer for trading records; append-only ledger
  api/         REST routes + /ws WebSocket fan-out
  engine.py    wires everything; 1s timer drives watchdog + broker.on_timer
  strategies/ regime/ scanner/ backtest/ analyst/ memory/ learning/   (later phases)
frontend/      React 19 + Vite + TS + Tailwind v4 + shadcn-style ui + Zustand +
               lightweight-charts + Recharts
vault/         Obsidian-compatible second brain (Phase 4)
```

Data flow today: provider → `MarketHub.on_tick` → `MarketState` + `CandleBuilder` → bus
`TICK` → `PaperBroker.on_tick` (fills/OCO/trailing/daily-loss) → `Journal` → bus
`TRADE/BOOK` → WebSocket → UI.
Planned flow: candle close → Scanner → Signal → Regime + Context → Analyst (LLM + memory) →
**RiskManager** → mode (manual/co-pilot/auto) → PaperBroker → Journal → Learning.

Key concepts:
- **Book** = market × segment × style (`crypto-futures-intraday`). Each book has its own
  currency, wallet, risk config and stats. The id must equal `f"{market}-{segment}-{style}"`.
- **Account** inside a book: `main` (human + agent share it), `shadow_baseline` and
  `shadow_rejected` (later phases). Balances are sums of the append-only ledger.
- **Actor** on every trade: `human | agent | baseline`.
- **Money** is `Decimal` end to end and travels as decimal strings in JSON. The UI only
  converts to float for display.
- **R-multiple** = net P&L after all charges ÷ initial risk (|entry − SL| × qty × contract size).
- Risk "risk amount" includes estimated round-trip fees (conservative).

## Run

```bash
make install          # uv sync + npm install
make dev              # live public crypto data (ccxt, binanceusdm) — needs internet
make dev-sim          # offline simulated feed (clearly labelled SIMULATED in the UI)
# UI: http://127.0.0.1:5173   API: http://127.0.0.1:8000/docs
```

## Test / quality gates (all must pass before a phase is done)

```bash
make check            # ruff + ruff format --check + mypy --strict + pytest (cov) + tsc + vitest
```
- Tests are deterministic: `ReplayClock`, in-memory SQLite, fixtures in
  `backend/tests/fixtures/`. **No test may hit a live API.**
- Coverage target ≥ 80% on `risk/`, `broker/`, `learning/`, `backtest/`.
- Worked numeric examples in broker/charges tests are computed by hand. If you change
  fill or fee maths, re-derive them. Never just update the expected numbers.

## Conventions

- Python 3.11+, full type hints, `mypy --strict` clean, ruff (line length 120).
- New risk rule ⇒ a pure function in `risk/rules.py` with a stable `Rxxx_NAME` id, added to
  `RULES`, with pass + fail tests.
- New market-data source ⇒ implement `MarketDataProvider`, push into `MarketHub`, no
  private endpoints, add fixture-based tests.
- UI: dark by default; never show a price without the stale check (`isPriceStale`); wrap
  widgets in `ErrorBoundary`. Chart colours: categorical slots from the validated palette
  (see `components/Charts.tsx`).

## Status

- **Phase 1 — Foundation: done.** Config, clocks, logging, DB, instrument registry, ccxt +
  simulated crypto data, staleness watchdog, risk manager, paper broker, charges, journal,
  REST + WS, UI shell (top bar + Home + manual order ticket).
- Phase 2 — strategies, regime, scanner, backtester, replay, baseline agent: next.
- Phases 3–6: see README roadmap.
