import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { Home } from "@/pages/Home";
import { api } from "@/lib/api";
import type { BookSummary, LLMStatus, SystemStatus } from "@/lib/types";
import { fmtLatency } from "@/lib/utils";
import { useApp } from "@/store/app";

const BOOK = "crypto-futures-intraday";

function llm(over: Partial<LLMStatus> = {}): LLMStatus {
  return {
    provider: "gemini",
    model: "gemini-3.8-flash",
    calls_today: 0,
    daily_budget: 20,
    enabled: true,
    ...over,
  };
}

function status(l: LLMStatus): SystemStatus {
  return {
    server_time: "",
    replay: false,
    kill_switch: { engaged: false },
    provider: { provider: "ccxt", exchange: "binanceusdm", connected: true },
    data: {},
    stale_after_seconds: 10,
    books: { [BOOK]: { status: "running", mode: "manual" } },
    llm: l,
    secrets: {},
  } as SystemStatus;
}

const book = {
  book_id: BOOK, currency: "USDT", mode: "manual", market: "crypto", segment: "futures", style: "intraday",
  balance: "1000", equity: "1000", starting_capital: "1000", total_pnl: "0", today_pnl: "0",
  est_exit_charges: "0", used_margin: "0", available_margin: "1000", positions: [], instruments: [],
} as unknown as BookSummary;

beforeEach(() => {
  vi.restoreAllMocks();
  // Home loads a few endpoints on mount; none of them matter to this panel
  vi.spyOn(api, "book").mockResolvedValue(book);
  vi.spyOn(api, "instruments").mockResolvedValue([]);
  vi.spyOn(api, "riskDecisions").mockResolvedValue([]);
  vi.spyOn(api, "equity").mockResolvedValue([]);
  vi.spyOn(api, "actorPnl").mockResolvedValue(null as never);
  useApp.setState({ conn: "open", ticks: {}, books: { [BOOK]: book }, riskFeed: [] });
});

function show(l: LLMStatus) {
  useApp.setState({ status: status(l) });
  render(<Home />);
}

describe("the analyst's daily stat", () => {
  it("shows calls against the daily budget, the average latency and the timeouts", () => {
    show(
      llm({
        calls_today: 12,
        today: {
          calls: 12, ok: 9, errors: 3, timeouts: 3, rate_limited: 0, cached: 0,
          avg_latency_ms: 8640, max_latency_ms: 44100, avg_queue_wait_ms: null,
          input_tokens: 18000, output_tokens: 2300,
        },
      }),
    );
    expect(screen.getByText("Analyst today")).toBeInTheDocument();
    expect(screen.getByText("12 / 20")).toBeInTheDocument();
    expect(screen.getByText("8.6s")).toBeInTheDocument();
    expect(screen.getByText("worst 44.1s")).toBeInTheDocument();
    const timeouts = screen.getByText("timeouts").parentElement;
    expect(timeouts).toHaveTextContent("3");
  });

  it("separates rate limiting from timeouts, because they call for different fixes", () => {
    show(
      llm({
        today: {
          calls: 30, ok: 5, errors: 25, timeouts: 5, rate_limited: 20, cached: 0,
          avg_latency_ms: 41000, max_latency_ms: 60000, avg_queue_wait_ms: 2500,
          input_tokens: 0, output_tokens: 0,
        },
      }),
    );
    expect(screen.getByText("rate limited").parentElement).toHaveTextContent("20");
    expect(screen.getByText("avg queue wait").parentElement).toHaveTextContent("2.5s");
  });

  it("says why the analyst stopped, so a quiet agent is never a mystery", () => {
    show(
      llm({
        paused: true,
        pause_reason: "provider rate limit (429): limit 20 requests per day — retry in 12h31m7s",
        today: {
          calls: 20, ok: 20, errors: 0, timeouts: 0, rate_limited: 0, cached: 0,
          avg_latency_ms: 7400, max_latency_ms: 9100, avg_queue_wait_ms: null,
          input_tokens: 0, output_tokens: 0,
        },
      }),
    );
    expect(screen.getByText(/20 requests per day/)).toBeInTheDocument();
  });

  it("shows a dash rather than a zero when nothing has reached the provider yet", () => {
    show(
      llm({
        today: {
          calls: 0, ok: 0, errors: 0, timeouts: 0, rate_limited: 0, cached: 0,
          avg_latency_ms: null, max_latency_ms: null, avg_queue_wait_ms: null,
          input_tokens: 0, output_tokens: 0,
        },
      }),
    );
    expect(screen.getByText("avg latency").parentElement).toHaveTextContent("—");
  });

  it("stays out of the way when the analyst is switched off and has done nothing", () => {
    show(llm({ provider: null, enabled: false, daily_budget: null }));
    expect(screen.queryByText("Analyst today")).not.toBeInTheDocument();
  });
});

describe("fmtLatency", () => {
  it("reads the way you'd say it", () => {
    expect(fmtLatency(840)).toBe("840ms");
    expect(fmtLatency(8640)).toBe("8.6s");
    expect(fmtLatency(62_000)).toBe("1m 2s");
    expect(fmtLatency(null)).toBe("—");
  });
});
