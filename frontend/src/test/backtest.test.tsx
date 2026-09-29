import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { ResultView } from "@/components/backtest/ResultView";
import { SignalsCard } from "@/components/SignalsCard";
import { api } from "@/lib/api";
import type { BacktestResult, Coverage, JobRow, MetricsRow, SystemStatus } from "@/lib/types";
import { BacktestPage } from "@/pages/Backtest";
import { useApp } from "@/store/app";

const m = (over: Partial<MetricsRow> = {}): MetricsRow => ({
  trades: 42, wins: 20, losses: 22, win_rate: 0.476, net_profit: "123.45", fees: "30.1", funding: "0", avg_r: 0.21,
  profit_factor: 1.31, max_drawdown: "40", max_drawdown_pct: 3.2, return_pct: 12.3, sharpe: 1.1, best_trade: "20",
  worst_trade: "-10", avg_hold_minutes: 55, ...over,
});

const result: BacktestResult = {
  kind: "walkforward", label: "Walk-forward, EMA pullback: out-of-sample results only", approximate: false, currency: "USDT",
  instrument: "crypto:simulated:BTC/USDT:USDT", period: { start: "2026-01-01T00:00:00Z", end: "2026-03-01T00:00:00Z" },
  data: { bars: 100, expected_bars: 100 }, agent: m(), baseline: m({ net_profit: "-50", trades: 40, avg_r: -0.1 }),
  buy_hold: { net_profit: "80", return_pct: 8, fees: "1", max_drawdown_pct: 12 },
  verdict: { meaningful: true, min_trades: 30, beat_baseline: true, beat_buy_hold: true },
  summary: "Net profit +123.45 USDT after 30.10 USDT of fees over 42 trades. Beat the baseline: YES.",
  equity: { agent: [{ t: "2026-01-02T00:00:00Z", v: "10" }], baseline: [{ t: "2026-01-02T00:00:00Z", v: "-5" }] },
  trades: [{ id: "t1", actor: "agent", direction: "long", qty: "0.01", entry: "65000", exit: "66000", stop: "64000", target: "67000",
    opened_at: "2026-01-02T00:00:00Z", closed_at: "2026-01-02T03:00:00Z", net_pnl: "9.5", fees: "0.5", r: "0.95",
    exit_reason: "target", note: "x" }],
  signals: 60, candles: [],
  folds: [{ fold: 1, train: ["2026-01-01T00:00:00Z", "2026-01-31T00:00:00Z"], test: ["2026-01-31T00:00:00Z", "2026-03-01T00:00:00Z"],
    params: { adx_min: 20, rr: 2 }, why: "best", oos_trades: 42, oos_net_profit: "123.45", oos_avg_r: 0.21,
    baseline_net_profit: "-50", baseline_avg_r: -0.1 }],
  grid_size: 4,
};

const status = {
  server_time: "", replay: false, kill_switch: { engaged: false }, provider: { provider: "ccxt", exchange: "binanceusdm", connected: true },
  data: {}, stale_after_seconds: 10, books: { "crypto-futures-intraday": { status: "running", mode: "manual" } },
  llm: { provider: null, calls_today: 0, daily_budget: null }, secrets: {},
} as SystemStatus;

beforeEach(() => {
  vi.restoreAllMocks();
  useApp.setState({ conn: "open", status, market: "crypto", segment: "futures", style: "intraday" });
});

describe("ResultView", () => {
  it("shows the plain-English summary, verdicts, OOS metrics and folds", () => {
    render(<ResultView r={result} />);
    expect(screen.getByTestId("plain-english")).toHaveTextContent("Beat the baseline: YES");
    expect(screen.getByText("Beat the random baseline")).toBeInTheDocument();
    expect(screen.getByText("Beat buy-and-hold")).toBeInTheDocument();
    expect(screen.getByText(/meaningful/)).toBeInTheDocument();
    expect(screen.getByText("Out-of-sample results")).toBeInTheDocument();
    const table = screen.getByTestId("metrics-table");
    expect(table).toHaveTextContent("+123.45");
    expect(table).toHaveTextContent("−50.00");
    expect(table).toHaveTextContent("+80.00");
    expect(screen.getByText("Walk-forward folds")).toBeInTheDocument();
    expect(screen.getByText("adx_min=20 rr=2")).toBeInTheDocument();
    expect(screen.getByTestId("bt-trades")).toHaveTextContent("target");
  });

  it("warns when a result is not meaningful or approximate", () => {
    render(<ResultView r={{ ...result, approximate: true, folds: undefined, verdict: { ...result.verdict, meaningful: false, beat_baseline: false } }} />);
    expect(screen.getByText(/APPROXIMATE/)).toBeInTheDocument();
    expect(screen.getByText(/NOT meaningful yet/)).toBeInTheDocument();
    expect(screen.getByText("Did NOT beat the random baseline")).toBeInTheDocument();
  });
});

describe("BacktestPage", () => {
  const cov: Coverage = { exchange: "binanceusdm", symbol: "BTC/USDT:USDT", timeframe: "5m", first: "2026-01-01T00:00:00Z",
    last: "2026-03-01T00:00:00Z", bars: 16992, expected_bars: 17000, missing_bars: 8, complete_pct: 99.95,
    gaps: [{ start: "2026-02-01T00:00:00Z", end: "2026-02-01T00:35:00Z", missing_bars: 8 }] };

  function mocks(job: JobRow) {
    vi.spyOn(api, "strategies").mockResolvedValue([{ id: "ema_pullback", name: "EMA pullback", description: "trend pullback",
      markets: ["crypto"], default_params: { rr: 2 }, param_grid: { rr: [1.5, 2] }, grid_size: 2 }]);
    vi.spyOn(api, "bookConfig").mockResolvedValue({ book: { instruments: ["BTC/USDT:USDT"], timeframes: { signal: "15m" } } });
    vi.spyOn(api, "coverage").mockResolvedValue(cov);
    vi.spyOn(api, "jobs").mockResolvedValue([]);
    const start = vi.spyOn(api, "startJob").mockResolvedValue({ run_id: "run_1" });
    vi.spyOn(api, "job").mockResolvedValue(job);
    const dl = vi.spyOn(api, "download").mockResolvedValue({ run_id: "run_dl" });
    return { start, dl };
  }

  it("shows data coverage with gaps and runs a backtest to a result", async () => {
    const done: JobRow = { id: "run_1", kind: "backtest", status: "done", progress: 1, message: null, spec: {}, error: null,
      created_at: "2026-03-01T00:00:00Z", finished_at: "2026-03-01T00:01:00Z", result: { ...result, kind: "backtest", folds: undefined } };
    const { start } = mocks(done);
    render(<BacktestPage />);
    await waitFor(() => expect(screen.getByTestId("coverage")).toHaveTextContent("8 missing bars"));
    expect(screen.getByTestId("coverage")).toHaveTextContent("gap");
    await waitFor(() => expect(screen.getByRole("button", { name: /Run backtest/ })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: /Run backtest/ }));
    expect(start).toHaveBeenCalledWith(expect.objectContaining({ kind: "backtest", strategy_id: "ema_pullback", symbol: "BTC/USDT:USDT", base_tf: "15m" }));
    expect(await screen.findByTestId("result-view")).toBeInTheDocument();
  });

  it("starts downloads and walk-forward jobs, and shows job errors", async () => {
    const failed: JobRow = { id: "run_1", kind: "walkforward", status: "error", progress: 0, message: null, spec: {},
      error: "no stored 5m data — download it first", created_at: "2026-03-01T00:00:00Z", finished_at: null };
    const { start, dl } = mocks(failed);
    render(<BacktestPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Download/ })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: /Download/ }));
    expect(dl).toHaveBeenCalledWith(expect.objectContaining({ exchange: "binanceusdm", symbol: "BTC/USDT:USDT", timeframe: "15m" }));
    await userEvent.click(screen.getByRole("radio", { name: "Walk-forward" }));
    expect(screen.getByText(/only the following unseen test window is reported/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Run walk-forward/ })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: /Run walk-forward/ }));
    expect(start).toHaveBeenCalledWith(expect.objectContaining({ kind: "walkforward", train_days: 60, test_days: 30 }));
    expect((await screen.findAllByText(/download it first/)).length).toBeGreaterThan(0);
  });

  it("blocks a bar size that does not divide the signal timeframe", async () => {
    mocks({ id: "x", kind: "backtest", status: "queued", progress: 0, message: null, spec: {}, error: null, created_at: "", finished_at: null });
    render(<BacktestPage />);
    await waitFor(() => expect(screen.getByLabelText("Bar size")).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByLabelText("Bar size"), "1h");
    expect(screen.getByText(/must evenly divide/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Run backtest/ })).toBeDisabled();
  });
});

describe("SignalsCard", () => {
  it("lists recent scanner signals", async () => {
    vi.spyOn(api, "signals").mockResolvedValue([{ id: "s1", ts: "2026-03-01T00:15:00Z", strategy_id: "supertrend_flip",
      direction: "short", entry_ref: "65000", stop_loss: "65500", target: "64000", setup: "Supertrend flipped down",
      decision: "take", executed: false, risk_rule_blocked: null, regime: { label: "trending_down" } }]);
    render(<SignalsCard bookId="crypto-futures-intraday" mode="manual" />);
    expect(await screen.findByText("supertrend_flip")).toBeInTheDocument();
    expect(screen.getByTestId("signals")).toHaveTextContent("trending down");
    expect(screen.getByText(/analysis only/)).toBeInTheDocument();
  });
});
