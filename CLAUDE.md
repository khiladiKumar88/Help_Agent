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
   The analyst answers only TAKE/SKIP plus *stricter-only* adjustments (a tighter stop, a smaller
   size); `analyst/schema.apply_adjustments` drops anything looser and records that it did.
   `risk/` still runs afterwards and has the final say.
3. **Fail safe.** Stale data (> `data.stale_after_seconds`), LLM error/rate-limit/timeout,
   invalid JSON, a schema violation, an exhausted daily budget, any exception ⇒ **NO TRADE**.
   Log it and keep running. Event-bus handlers are isolated so one failure never stops the
   engine. The UI shows disconnected/stale states. There is no "LLM failed, so take it anyway"
   path: without an analyst the agent modes are refused and the UI says why.
4. **Everything is logged:** risk decisions (`risk_decisions`), orders, fills, trades,
   ledger, audit log (`audit_log`), and every LLM call (`llm_calls`: prompt version, prompt,
   response, latency, tokens, failure reason). Lessons get logged once Phase 4 lands.
5. **Secrets only in `.env`** (`.env.example` is committed). Logging passes through
   `core/logging.RedactingFilter`; the API exposes secret *presence* only. Nothing secret ever
   goes into a prompt (test-enforced).
   - **Read secrets through `Settings.secret(name)`, never `os.environ` directly.**
     pydantic-settings loads `.env` into `Settings` and does NOT export it into the process
     environment, so `os.environ` cannot see a key the user put where the docs say to put it.
     This bit once: `GeminiProvider` read `os.environ`, so the analyst refused to start from a
     documented `.env` setup while every test passed (the fakes never needed a key).
     `analyst.factory.resolve_api_key` is the one place that resolves it: `.env` first, then the
     environment, which is what makes a custom `llm.api_key_env` name work.
6. **No lookahead.** Everything reads time from the injected `Clock`. Use `ReplayClock` for
   replays and tests. Strategies/backtests only ever see **closed** candles (`CandleBuilder`
   emits closed bars only; `ohlcv_to_candles` drops the forming bar).

7. **Honest backtests.** Backtests/replays read only the local `HistoryStore` (never an exchange
   API), always pay the book's fees + slippage (zero costs are refused), use small parameter grids
   (≤ 16 combos, enforced), report walk-forward results on out-of-sample windows only, always
   compare against the random baseline and buy-and-hold, and flag < `min_trades` (30) as not
   meaningful. Synthetic option prices are labelled APPROXIMATE.
   - **Fills happen at the level you asked for, not at the bar's extreme.** A stop-market order
     fills at its trigger plus the slippage model (`fills.stop_fill`); only a gap — a price
     flagged `Tick.gap` that is already beyond the stop — fills worse. Filling off the raw tick
     made every stop-out cost ~1.11R while targets still paid their full +rr*R: a one-sided bias
     of about -0.1R per trade. Targets are resting limits and fill at their own price.
   - **Independent windows are never reported as one account.** Each walk-forward fold runs from
     a fresh copy of the capital, so `metrics.combine_metrics` sums counts and money, pools R and
     win rate per trade, and reports drawdown/return % as the worst/average SINGLE window. Pooling
     them against one capital produced drawdowns above 100%, which cannot exist.
   - **The simulator's fairness is measured, not assumed.** `tests/backtest/test_fairness*.py`
     put random entries on a driftless walk and assert expectancy ~0R with zero costs and
     exactly -(cost in R) with costs on. They are the only tests allowed to switch costs off
     (`run_engine(allow_zero_costs=True)`), and a test asserts every other path still refuses it.
   - **Slippage is measured per instrument.** `SlippageConfig.per_instrument` entries carry
     `observed_spread_bps`, `source` and `as_of`, like the fee files. Keep the assumption worse
     than the observation; the book-wide fallback stays wide so an unmeasured instrument costs
     MORE, never less.

Also: never hardcode lot sizes, tick sizes, expiries or fee rates. They come from the
instrument master (ccxt markets / Angel One scrip master) or `config/`. Fee files carry
`source_url`, `as_of` and `verified`. Only a human flips `verified: true`.

## Architecture

```
config/                 YAML: default.yaml, books/*.yaml, charges/*.yaml, calendars/*.yaml
backend/papermind/
  core/        config (pydantic-settings + YAML), clock (Real/Replay), events (async bus),
               logging (JSON + redaction), money (Decimal helpers), types, serde
  db/          SQLAlchemy 2 models; SQLite WAL (Postgres-swappable: DecimalText -> NUMERIC);
               migrate.py runs Alembic (backend/migrations) on startup, stamps legacy Phase-1 DBs
  instruments/ registry: ccxt market -> Instrument (persisted, refreshed daily)
  data/        MarketDataProvider ABC; CcxtProvider (ws->poll fallback, public only);
               SimulatedProvider (seeded offline feed); MarketHub; CandleBuilder; watchdog;
               history.py: HistoryStore (separate history.db cache) + paginated incremental
               HistoryDownloader + SimulatedHistoryExchange (deterministic offline history)
  risk/        rules.py (R000-R014, pure), manager.py (fixed chain + sizing), session.py
  broker/      PaperBroker (fills, slippage, OCO SL/target, trailing, funding, square-off,
               expiry, daily-loss halt, kill switch, crash recovery); charges; fills
  journal/     Journal: the only DB writer for trading records; append-only ledger
  api/         REST routes + /ws WebSocket fan-out
  strategies/  indicators (numpy), FeatureFrame (memoized per bar), Strategy ABC, 6 built-ins
  regime/      rule-based regime detector + time-of-day buckets
  scanner/     Scanner (closed candle -> strategies -> Signal), AgentExecutor (Decider ->
               sizing -> broker.submit), BaselineAgent (random entries, shadow account)
  backtest/    HistoricalReplayProvider, runner (backtest / walk-forward / replay), metrics
               (+ combine_metrics for multi-window runs), diagnostics (where the R went: R by
               exit reason, winner vs loser, slippage and fees in R, stop overshoot, stop+target
               in one bar), options (Black-Scholes synthetic pricing), jobs (worker thread)
  engine.py    wires everything; EngineOptions switch live vs replay (same class!)
  llm/         LLMProvider ABC; GeminiProvider (google-genai Interactions API); FakeProvider
               (deterministic, used by every test); RateLimiter (token bucket + daily budget)
  analyst/     LLMAnalyst as a Decider; prompts/*.md (versioned); schema.py (strict JSON +
               stricter-only adjustments); context.py (compact prompt, Phase-4 hooks); factory.py
  memory/ learning/   (later phases)
frontend/      React 19 + Vite + TS + Tailwind v4 + shadcn-style ui + Zustand +
               lightweight-charts + Recharts
vault/         Obsidian-compatible second brain (Phase 4)
```

Data flow: provider → `MarketHub` → `MarketState` + `CandleBuilder` → bus `TICK` →
`PaperBroker.on_tick` (fills/OCO/trailing/daily-loss). Bus `CANDLE` (closed only) → `Scanner`
(FeatureFrame → regime → strategies) → `SIGNAL` → `AgentExecutor` (Decider: `LLMAnalyst`, or
`RuleDecider` when the LLM is off) → `AGENT_DECISION` (→ `BaselineAgent`) → sizing →
**RiskManager** inside `broker.submit` → PaperBroker → Journal → WebSocket → UI.

Mode routing in `AgentExecutor`: **Manual** records the signal and trades nothing; **Co-pilot**
turns a TAKE into a pending card you approve or reject (it expires after
`copilot.card_ttl_bars`); **Auto** places it itself. `force_execute` (backtests/replays) always
executes. The analyst is called once per signal — never per tick.

**`EventBus.publish` awaits its handlers in sequence**, so whatever the analyst spends inside
the SIGNAL handler is time the tick path is not running — including `PaperBroker.on_tick`, which
is what evaluates stops and targets. `llm.timeout_seconds` is a budget **per attempt**, so
`limits.max_retries` multiplies it: at 60s and 3 retries the worst case is 4 x 60s plus up to
14s of backoff, about 4.2 minutes. That is acceptable today because a stall only delays fill
evaluation (the paper broker replays the ticks it then receives, it does not drop them), but it
is the reason to keep `max_retries` small, and the thing to fix first if the analyst ever needs
a longer budget: move the call off the bus rather than raising the timeout again.

Replay/backtest = `Engine(cfg, clock=ReplayClock, provider=HistoricalReplayProvider,
options=EngineOptions(drive_timers=False, force_execute=True, ...))`. Each stored bar becomes 4
ticks (open, extremes, close — bullish bars assumed O→L→H→C, bearish O→H→L→C) then a candle
close; market orders placed at a close fill on the next bar's open. A test asserts a one-day
replay and a backtest of that day produce identical agent trades.

Key concepts:
- **Book** = market × segment × style (`crypto-futures-intraday`). Each book has its own
  currency, wallet, risk config and stats. The id must equal `f"{market}-{segment}-{style}"`.
- **Account** inside a book, each with its own wallet and its own daily-loss halt; balances are
  sums of the append-only ledger:
  - `main` — human + agent share it
  - `shadow_baseline` — random entries, same risk and frequency
  - `shadow_raw` — EVERY signal, unfiltered. This is the control group that says whether the
    analyst's TAKE/SKIP filter beats taking everything. It only runs when an analyst is actually
    filtering; with the LLM off the agent already *is* the raw strategy.
  - `shadow_rejected` — Co-pilot cards you rejected, so a veto's cost is measurable
  Shadow trades are tagged `actor=agent`, so anything comparing actors must key on
  (account, actor) — see `SERIES_SOURCES` in `api/routes_books.py`. `/api/books/{id}/edge`
  reports all five side by side.
- **Baseline**: for each signal the agent TAKES, a random-direction entry after 0..N bars with
  the same stop distance, R:R, sizing and risk rules (seeded). The agent must beat it.
- **Actor** on every trade: `human | agent | baseline`.
- **Money** is `Decimal` end to end and travels as decimal strings in JSON. The UI only
  converts to float for display.
- **R-multiple** = net P&L after all charges ÷ initial risk (|entry − SL| × qty × contract size).
- Risk "risk amount" includes estimated round-trip fees (conservative).

## Run

Windows (`run.ps1`) and macOS/Linux (`make`) take the same command names. Keep them in step:
a new command belongs in both.

```powershell
.\run.ps1 install     # uv sync + npm install
.\run.ps1 dev         # live public crypto data (ccxt, binanceusdm) — needs internet
.\run.ps1 dev-sim     # offline simulated feed (clearly labelled SIMULATED in the UI)
.\run.ps1 help        # every command;  add -DryRun to print a command without running it
```

```bash
make install          # the same, via make
make dev
make dev-sim
# UI: http://127.0.0.1:5173   API: http://127.0.0.1:8000/docs
```

`dev` / `dev-sim` run both servers in one console and stop both on Ctrl+C. `run.ps1` derives
every path from `$PSScriptRoot`, so a checkout under a folder with spaces works;
`tests/test_run_ps1.py` asserts that (and the command set) by running it with `-DryRun`.

## Test / quality gates (all must pass before a phase is done)

```powershell
.\run.ps1 check       # ruff + ruff format --check + mypy --strict + pytest (cov) + tsc + vitest
```

```bash
make check            # the same
```
- Migrations: change models → `cd backend && uv run alembic revision --autogenerate -m "..."`,
  review the file, then `tests/db/test_migrations.py` must pass (head == models).
- Tests are deterministic: `ReplayClock`, in-memory SQLite, fixtures in
  `backend/tests/fixtures/`. **No test may hit a live API.** The LLM is always `FakeProvider`
  or a stubbed SDK client. The single live smoke test (`tests/llm/test_live_gemini.py`) carries
  the `live` marker, which `addopts = -m 'not live'` excludes — run it deliberately with
  `PAPERMIND_LIVE_LLM=1 uv run pytest -m live`.
- Coverage target ≥ 80% on `risk/`, `broker/`, `learning/`, `backtest/`.
- Worked numeric examples in broker/charges tests are computed by hand. If you change
  fill or fee maths, re-derive them. Never just update the expected numbers.

## Conventions

- Python 3.11+, full type hints, `mypy --strict` clean, ruff (line length 120). mypy has no
  pinned `python_version`, so it checks against whichever interpreter you run it on (that is the
  one whose dependencies are installed); ruff's `target-version = "py311"` guards the syntax floor.
- New risk rule ⇒ a pure function in `risk/rules.py` with a stable `Rxxx_NAME` id, added to
  `RULES`, with pass + fail tests.
- New market-data source ⇒ implement `MarketDataProvider`, push into `MarketHub`, no
  private endpoints, add fixture-based tests.
- New LLM provider ⇒ subclass `llm.base.LLMProvider`, raise an `LLMError` subclass for EVERY
  failure (the analyst maps each one to NO TRADE), register it in `analyst/factory.build_provider`,
  and test it with a stubbed client — never the network.
- Changing the analyst's behaviour ⇒ add a new `analyst/prompts/<version>.md` and bump
  `llm.prompt_version`; the version used is stored on every decision, so old decisions stay
  explainable. Never edit a shipped prompt in place.
- The model id and every rate/budget limit come from `llm` config. Google publishes free-tier
  limits per account in AI Studio, not in its docs, so they can never be hardcoded.
- New strategy ⇒ subclass `Strategy` in `strategies/builtin.py` (or a new module registered in
  `STRATEGIES`), use only `ctx.frame` (closed candles), declare `default_params` and a SMALL
  `param_grid`, return levels via `Strategy.build` (tick-rounded, sanity-checked), add tests.
- Never put reserved LogRecord names (`msg`, `message`, `args`, ...) in `extra={}` (test enforced).
- UI: dark by default; never show a price without the stale check (`isPriceStale`); wrap
  widgets in `ErrorBoundary`. Chart colours: categorical slots from the validated palette
  (see `components/Charts.tsx`).
- Chart maths lives in `lib/` as pure functions (`lib/priceRange.ts`), not inside the
  `lightweight-charts` effects, so it is testable directly. `test/setup.ts` shims `matchMedia`,
  `ResizeObserver` and a no-op 2D canvas context, which is what lets chart components mount in
  jsdom at all (`test/priceChart.test.tsx`); assert against the DOM they render, never pixels.
- The order preview shows the assumed slippage from the same code path the fill uses
  (`broker/fills.estimate_entry_fill`), so the preview can never quote a model the broker
  does not apply.

## Status

- **Phase 1 — Foundation: done.** Config, clocks, logging, DB, instrument registry, ccxt +
  simulated crypto data, staleness watchdog, risk manager, paper broker, charges, journal,
  REST + WS, UI shell (top bar + Home + manual order ticket).
- **Phase 2 — done.** Alembic, history store + downloader + coverage, indicators, regime,
  6 strategies, scanner, rules agent, random baseline, backtester, walk-forward (OOS only),
  day replay, metrics + plain-English verdicts, synthetic option pricer, Backtest/Replay page.
- **Windows parity + Phase-2 leftovers — done.** `run.ps1` (install/dev/dev-sim/test/check,
  safe with spaces in paths); the price chart fits its scale to the SL/target of open positions
  and marks levels too far away to fit; the order ticket resets and confirms "position opened"
  with the real fill price; the order preview states the slippage the model assumed.
  Windows fix: Alembic's config is `%`-escaped, because SQLAlchemy percent-encodes Windows
  paths in a URL and ConfigParser interpolation choked on it.
- **Phase 3 — done.** Gemini via the official `google-genai` SDK behind `LLMProvider`
  (model + limits from config); token-bucket rate limiter + daily budget that PAUSES the agent
  rather than trading without a verdict; strict JSON validated by Pydantic; versioned prompts;
  every call logged; response cache by prompt hash so replays cost no quota; `shadow_raw` and
  `shadow_rejected` accounts plus `/edge` so the analyst's filter is measurable; Co-pilot cards
  with expiry, Auto behind a confirm dialog; Live, Journal and Settings pages; backtests/replays
  take `llm=off|fake|on` (default off) and label `on` as possible hindsight bias.
- Phases 4–6: see README roadmap. The Phase-4 hooks are already in `analyst/context.py`
  (`LessonSource`, `EpisodeSource`) and return empty for now.

- **Simulator honesty pass — done.** Fixed the stop-fill bias (stops filled at the bar's
  extreme, costing ~0.1R per trade) and the walk-forward aggregate (independent windows pooled
  against one capital, giving >100% drawdowns). Added `backtest/diagnostics.py`, the fairness
  test suite, and per-instrument slippage calibrated against measured order-book spreads.

### Known gaps worth fixing before they mislead anyone

- A **live** feed is assumed continuous between ticks: only the replay flags `Tick.gap`, so a
  genuine jump on a live feed would fill a stop at its level rather than at the jump price.
  Fine for a 1/sec feed on liquid perps; revisit for illiquid instruments or sparse feeds.
- After the stop-fill fix, a finer base timeframe barely changes a backtest, because exits now
  land on their own levels rather than on bar extremes. The remaining resolution-sensitivity is
  the case where stop and target sit inside ONE bar and OHLC cannot say which came first;
  `diagnostics.both_levels_in_one_bar_pct` reports how often that happened (0-1% for
  `ema_pullback` on 15m BTC/ETH). Finer bars still matter for strategies whose levels sit closer
  together.
