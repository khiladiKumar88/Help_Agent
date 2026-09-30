# PaperMind — Original Build Spec

> Master spec for this project. Phases 1–2 are done; see CLAUDE.md and README.md for the current state. Later user messages may add to or override parts of this spec.

You are building **PaperMind**, a self-improving AI paper-trading agent with a web UI. It trades **only on paper** (simulated money) across **Indian markets (Nifty/BankNifty index futures + options)** and **Crypto (spot, perpetual futures, BTC/ETH options)**. It learns from its own trades using a "second brain" memory system and statistical learning, and it must be built to be solid, testable and safe.

Work in phases. **After each phase: run all tests, show me a short plain-English summary of what works, and STOP for my go-ahead before starting the next phase.** Do not skip ahead. If an external API detail is uncertain (endpoints, lot sizes, expiry days, charges, model names), check the official docs first and never hardcode guesses. Load these from instrument masters or config, and tell me what you verified.

## 0. Non-negotiable rules

1. **Paper only.** Never implement or call any real order-placement endpoint (Angel One order APIs, exchange trading APIs with private keys). Use market data APIs only. Add a test that fails if any order-placement function from a broker SDK is imported.
2. **The LLM never controls risk.** Risk rules are pure Python code the LLM cannot change, override or bypass.
3. **Fail safe.** If data is stale (no tick for > N seconds, configurable), the LLM errors or is rate-limited, JSON output is invalid, or anything unexpected happens, the answer is **NO TRADE**. Log it and keep running.
4. **Everything is logged:** every signal, LLM call (prompt, response, latency, tokens), decision, trade and lesson change.
5. **Secrets** live in `.env` only (with `.env.example` committed). Never log secrets.
6. **No lookahead bias** anywhere. Backtests and replays may only see data up to the current simulated timestamp.

## 1. Tech stack

- **Backend:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy + SQLite (WAL mode; keep it swappable to Postgres), APScheduler for jobs, asyncio.
- **Realtime:** a WebSocket from the backend to the UI for ticks, signals, positions and agent status.
- **Frontend:** React + Vite + TypeScript + Tailwind + shadcn/ui, TradingView `lightweight-charts` for price charts, Recharts for stats charts, Zustand for state.
- **LLM:** Google **Gemini API free tier** behind an `LLMProvider` interface (so Groq/Claude/Ollama can be swapped by config). Model id comes from config; check the current free-tier Flash model in Google's docs. Add a **token-bucket rate limiter + daily call budget** that matches the free-tier limits (configurable), with exponential backoff. Use structured JSON output and validate it with Pydantic.
- **Embeddings:** local `fastembed` (or sentence-transformers) so Gemini quota is saved for reasoning. **Vector store:** LanceDB or Chroma (local, file-based).
- **Market data:**
  - India: **Angel One SmartAPI** (`smartapi-python`). Login with API key, client code, PIN and TOTP from `.env`. Use WebSocket V2 for live ticks and the instrument master (scrip master JSON) for tokens, lot sizes, strikes and expiries, refreshed daily. Use the quote/option Greeks endpoints where available.
  - Crypto: **ccxt** for spot and perpetuals (exchange configurable: Binance / Bybit / Delta Exchange India), public endpoints only. BTC/ETH options data comes from Delta Exchange India or Deribit public API (configurable).
  - News: RSS feeds (configurable list: ET Markets, Moneycontrol, Business Standard markets, CoinDesk, Cointelegraph), deduplicated, with timestamps.
  - Context: India VIX via Angel One; crypto funding rates and open interest via ccxt.
- **Infra:** `docker-compose.yml` (backend + frontend), plus a `make dev` / plain run path without Docker. Tests with pytest (backend) and Vitest (frontend). Ruff + mypy for Python.

Create a `CLAUDE.md` at the repo root describing the architecture, the rules in section 0, how to run and how to test. Keep it updated every phase.

## 2. Architecture (modules)

```
backend/
  core/        config (pydantic-settings, YAML + .env), clock (real / replay), logging, event bus
  data/        MarketDataProvider interface; AngelOneProvider, CcxtProvider, CryptoOptionsProvider,
               NewsProvider; candle builder (1m→5m/15m/1h/4h/1D); staleness watchdog
  instruments/ instrument registry (market, segment, symbol, lot size, tick size, expiry, strike)
  strategies/  Strategy plugin interface + built-in strategies (see §3)
  regime/      regime detector
  scanner/     runs enabled strategies on candle close → candidate Signals
  analyst/     LLM analyst (builds context, retrieves memory, calls LLM, validates JSON)
  risk/        RiskManager (pure code)
  broker/      PaperBroker (fills, slippage, charges, SL/target OCO, EOD square-off, expiry)
  journal/     trade + snapshot persistence
  memory/      Library (RAG), Episodes (similar-trade retrieval), Playbook (lessons vault)
  learning/    reflection job, lesson evaluator, promotion engine, contextual bandit, baseline agent
  backtest/    event-driven backtester + walk-forward, reusing the SAME strategy/risk/broker code
  api/         REST + WebSocket routes
frontend/
vault/         Obsidian-compatible markdown second brain (playbook/, library/, reflections/)
```

**Core flow (event-driven):**
candle close → Scanner (strategies) → candidate Signal → Regime + Context → Analyst (LLM + memory) → RiskManager → depending on mode: auto-execute, or send to the UI for approval → PaperBroker → Journal → (later) Learning.

## 3. Markets, segments, styles

- A **Market** (`india` | `crypto`) × **Segment** (`spot` | `futures` | `options`) × **Style** (`intraday` | `swing`) is a configurable **"book"**. Each book has its own paper wallet (INR for India, USDT for crypto), its own risk config and its own stats. Never mix currencies or books.
- India intraday: trade 09:20–15:00 IST, forced square-off at 15:15, no new entries after 15:00. Use the NSE holiday calendar (configurable file). Expiry days come from the instrument master, not hardcoded weekdays.
- Default timeframes: India intraday 5m (1m for execution checks), crypto intraday 15m, swing 4h/1D. All configurable.
- **Built-in strategies** (each a plugin with params, able to emit long/short signals with entry, SL, target and a setup description):
  1. Opening Range Breakout (India)
  2. VWAP reclaim / rejection
  3. EMA 9/21 trend pullback with ADX filter
  4. Supertrend flip
  5. RSI mean-reversion in ranging regime
  6. Crypto: funding-rate extreme fade (context-filtered)
- **Options layer:** when a signal is directional on an index/underlying and the book is options, pick the contract by rules: nearest valid expiry (avoid expiry-day entries unless enabled), ATM/1-step-ITM, premium ≤ configured cap. Put option SL/target on the premium, derived from the underlying levels via delta. Show the option chain context (OI, change in OI, PCR, IV) to the analyst as extra information.
- **Regime detector** (rule-based v1): trending-up / trending-down / ranging / high-volatility, from ADX, ATR percentile, EMA slope and India VIX / crypto realized vol. Also tag time-of-day bucket and expiry-day flag.

## 4. The LLM Analyst

- It is called **only** when the scanner emits a candidate signal, plus scheduled reflection jobs. It never runs per tick.
- Input context (kept compact): signal and setup, last N candles summary + key indicators, regime, relevant news headlines (last X hours), option chain summary if relevant, **top-k active playbook lessons matching this context**, and the **5 most similar past trades** (vector search on the context snapshot) with their outcomes.
- Output (strict JSON, Pydantic-validated): `decision` (TAKE | SKIP), `confidence` (0–100), `reasoning` (short plain English), `lessons_applied` (ids), `risks_noted`, `suggested_adjustments` (only within allowed bounds, e.g. tighter SL; never looser than the strategy/risk rules).
- Confidence threshold is configurable (default 70). Below it the answer is SKIP.
- Keep prompts in versioned template files (`analyst/prompts/*.md`) and store the prompt version with every decision.

## 5. Risk Manager (pure code, fully unit-tested)

Per-book config, defaults for the India options book:
- Capital ₹20,000; max 1 lot; option premium ≤ ₹150; max risk per trade ₹2,000; max 2 trades/day; daily loss limit (configurable, default ₹3,000) that halts the book for the day; max 1 open position per book; no averaging down.
- Crypto defaults: configurable % risk per trade (default 1%), max leverage cap (default 3x), max open positions, daily loss limit.
- A global **Kill Switch** (API + UI) that immediately halts all books and closes all paper positions at market.
- Every rejection is logged with the exact rule that blocked it and shown in the UI.

## 6. Paper Broker (realistic)

- Fill at next tick/candle with a configurable **slippage model** (spread-based for options; bps for crypto). Use bid/ask when available, otherwise LTP ± half the estimated spread.
- **Charges:** put brokerage, STT, exchange transaction charges, SEBI fee, stamp duty and GST for Indian F&O in a config file, sourced from Angel One's current charge sheet. Mark them "verify" and show them in Settings. Crypto: maker/taker fees + funding payments for perps.
- SL/target as OCO, optional trailing SL, intraday auto square-off, option expiry settlement.
- Positions are marked-to-market live. P&L is always reported **after charges**, plus the R-multiple.

## 7. Trading modes + books of record

- Modes per book: **Manual** (user places paper trades from the UI; agent only shows analysis), **Co-pilot** (agent signals appear as cards with Approve / Reject; they auto-expire after N minutes), **Auto** (agent executes within risk rules).
- Every trade is tagged with an `actor`: `human` | `agent` | `baseline`. Stats are always separable by actor.
- In Co-pilot, log the user's approve/reject plus what would have happened if the rejected trade had been taken (shadow-tracked). This is learning data.
- **Baseline agent:** runs in shadow on every book. It takes random entries with the same risk rules and the same trade frequency. The agent must beat this to claim it has learned anything.

## 8. Memory — the Second Brain

1. **Library (semantic):** ingest curated trading knowledge (markdown/PDF/URLs I add into `vault/library/`) with chunking + embeddings + source metadata. Retrieval is used by the analyst and by reflection. Add a UI to upload docs.
2. **Episodes (episodic):** every signal (taken or skipped) and every trade stores a full context snapshot (JSON) plus an embedding of a text summary, for similar-situation retrieval.
3. **Playbook (procedural):** each lesson is a markdown file in `vault/playbook/` with YAML frontmatter: `id, title, status (hypothesis|probation|active|retired), book, strategy, conditions, action (skip|reduce_size|require_confirmation|prefer), created, evidence_n, wins, losses, expectancy_R, baseline_expectancy_R, last_evaluated, source (reflection|human)`, plus a plain-English body and `[[links]]` to related lessons and trades.
   - **Lessons must be machine-checkable.** `conditions` uses a small declarative DSL over snapshot fields (e.g. regime, strategy, time bucket, VIX range, expiry-day flag, RSI range, news-sentiment flag). The evaluator can then test any lesson against historical episodes objectively.
   - The user can create, edit, pin, disable or delete lessons from the UI. Human-authored lessons are marked `source: human`.
   - SQLite keeps an index mirroring the vault for fast queries. The vault is the source of truth, and both stay in sync.

## 9. Learning loop (the "improves itself" part)

1. **Daily reflection job** (after India close, and daily 00:00 UTC for crypto). The LLM reviews the day's trades, skips and rejected signals, retrieves related lessons + library knowledge, and writes a reflection note to `vault/reflections/YYYY-MM-DD.md`. It proposes **at most 3 new lessons** as `hypothesis`, each with DSL conditions. It must separate *decision quality* from *outcome luck*.
2. **Lesson evaluator (pure code, no LLM):** tests each hypothesis against all historical episodes matching its conditions (plus the backtester when history is insufficient).
3. **Promotion engine (pure code):** hypothesis → probation only if at least N matching episodes exist (default 20) and the effect vs the no-lesson counterfactual is positive with a conservative bound (e.g. bootstrap CI on expectancy in R). Probation → active after M more live-paper trades confirm it (default 20). Active → retired if rolling performance decays. Log every transition with its numbers.
4. **Contextual bandit:** Thompson sampling over (strategy × regime × book) using the R-multiple as reward, to decide which strategies are allowed/prioritized in the current regime. Show its current beliefs in the UI.
5. **Backtest + walk-forward harness:** any new strategy or param change must pass walk-forward on historical data before it can run on paper. It reuses the exact strategy, risk and broker code.
6. **Replay mode:** replay any historical day through the full pipeline at accelerated speed (simulated clock). This is used for testing, demos and fast learning.

## 10. UI (clean, dark by default, easy to understand)

**Top bar (always visible):** Market switch (Crypto | India) · Segment switch (Spot | Futures | Options) · Style (Intraday | Swing) · Mode toggle (Manual | Co-pilot | Auto) · Agent status pill (Running / Thinking / Rate-limited / Market closed / Halted) · LLM quota used today · big red **Kill Switch** with a confirm modal.

**Pages:**
1. **Home:** paper balance, today's P&L, open positions, equity curve, and a "You vs Agent vs Baseline" comparison chart for the selected book.
2. **Live:** candlestick chart with signal markers, entry/SL/target lines and VWAP/EMAs, plus a signal-card feed beside it. Each card shows setup, entry/SL/target, confidence, a plain-English "why", lessons applied, and Approve/Reject in Co-pilot. For options, show the option chain snippet and the chosen strike. Include a manual order panel (paper) for Manual mode.
3. **Journal:** table of all trades and skipped signals, filterable by book/actor/strategy/result. The detail drawer shows the full snapshot, the LLM reasoning, the chart at entry and the outcome.
4. **Brain:** playbook lessons as cards grouped by status, with evidence count, win rate, expectancy vs baseline and a status timeline, plus edit/pin/disable/delete. Also shows daily reflection notes, library documents (upload) and bandit beliefs per regime.
5. **Performance:** win rate, profit factor, expectancy (R), max drawdown, Sharpe, broken down by strategy × regime × time of day × actor, with charts.
6. **Backtest / Replay:** pick book + strategy + date range, run, and view results in plain English + charts.
7. **Settings:** risk rules per book, enabled strategies + params, charges table, API keys status (never show values), LLM model + rate limits, news feeds, trading hours / holidays.

The UI must stay usable when the backend is down (clear "disconnected" state) and must never show stale prices without a stale warning.

## 11. Phases (stop after each)

- **Phase 1 — Foundation:** repo scaffold, config, clock (real + replay), logging, DB models, CLAUDE.md, instrument registry, Crypto data via ccxt (candles + live), staleness watchdog, PaperBroker + charges + RiskManager with thorough unit tests, Journal. Minimal UI shell with top bar + Home showing a manually placed paper trade on crypto.
  *Done when:* I can place a manual paper trade on BTC perp from the UI and see live P&L after fees, and the risk/broker/charges tests pass.
- **Phase 2 — Strategies + scanner + backtester:** strategy plugins, regime detector, scanner, event-driven backtester + walk-forward, replay mode, baseline agent. Backtest/Replay page.
  *Done when:* I can backtest each strategy on crypto history, replay a past day, and see results vs baseline in plain English.
- **Phase 3 — LLM Analyst + Co-pilot/Auto:** Gemini provider with rate limiter/budget, analyst prompts + JSON validation, Live page with signal cards, Co-pilot approve/reject with shadow tracking, Auto mode.
  *Done when:* in replay mode the agent produces signal cards with reasoning, Co-pilot and Auto both work, and forcing an LLM failure results in NO TRADE.
- **Phase 4 — Second brain + learning loop:** Library ingestion, episode embeddings + similar-trade retrieval, Playbook vault + DSL + evaluator + promotion engine, daily reflection, contextual bandit, Brain + Performance pages.
  *Done when:* running replay over 30+ past days produces reflection notes, hypotheses get promoted/rejected with visible numbers, and the Brain page shows it.
- **Phase 5 — Indian markets:** Angel One SmartAPI login (TOTP), WebSocket ticks, instrument master sync, Nifty/BankNifty futures + options books, option contract selection, option chain context, market hours/holidays/expiry handling, India VIX, and the India options risk defaults from §5.
  *Done when:* during market hours the India options book runs in Co-pilot with live data, respects all risk rules, and auto-squares off at 15:15.
- **Phase 6 — Hardening:** crypto options book, news sentiment tagging, error recovery/reconnect logic, Docker compose, backup of DB + vault, README with setup steps, end-to-end tests of the full pipeline in replay mode.

## 12. Quality bar

- Type hints everywhere; ruff + mypy clean; pytest coverage ≥ 80% on `risk/`, `broker/`, `learning/`, `backtest/`.
- Deterministic tests via the replay clock and recorded fixtures. No test hits live APIs.
- Each phase ends with a short report: what's done, how to run it, what I should verify manually, and known limitations.

Start with Phase 1. First, show me the proposed folder structure and data models and wait for my OK before writing code.
