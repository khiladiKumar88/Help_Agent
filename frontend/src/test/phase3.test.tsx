import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { expiresIn, rewardRisk, SignalCardFeed } from "@/components/SignalCardFeed";
import { LlmQuota, TopBar } from "@/components/TopBar";
import { api } from "@/lib/api";
import type { BookSummary, LLMStatus, SettingsView, SignalCard, SignalDetail, SignalRow, SystemStatus } from "@/lib/types";
import { filterParams, Journal } from "@/pages/Journal";
import { label, Settings } from "@/pages/Settings";
import { useApp } from "@/store/app";

const BOOK = "crypto-futures-intraday";

const llm = (over: Partial<LLMStatus> = {}): LLMStatus => ({
  provider: "gemini",
  model: "gemini-3.8-flash",
  calls_today: 7,
  daily_budget: 200,
  enabled: true,
  remaining: 193,
  paused: false,
  ...over,
});

const status = (over: Partial<SystemStatus> = {}): SystemStatus =>
  ({
    server_time: "",
    replay: false,
    kill_switch: { engaged: false },
    provider: { provider: "ccxt", exchange: "binanceusdm", connected: true },
    data: {},
    stale_after_seconds: 10,
    books: { [BOOK]: { status: "running", mode: "copilot" } },
    llm: llm(),
    secrets: {},
    ...over,
  }) as SystemStatus;

const book = (over: Partial<BookSummary> = {}): BookSummary =>
  ({
    book_id: BOOK,
    currency: "USDT",
    mode: "copilot",
    market: "crypto",
    segment: "futures",
    style: "intraday",
    positions: [],
    instruments: ["i1"],
    ...over,
  }) as unknown as BookSummary;

const card = (over: Partial<SignalCard> = {}): SignalCard =>
  ({
    id: "sig_1",
    ts: "2026-10-01T10:00:00Z",
    strategy_id: "ema_pullback",
    direction: "long",
    entry_ref: "65000",
    stop_loss: "64000",
    target: "67000",
    setup: "pullback to EMA21",
    decision: "take",
    executed: false,
    risk_rule_blocked: null,
    regime: { label: "trending_up" },
    confidence: 88,
    reasoning: "Trend intact and the pullback held.",
    risks_noted: ["ADX may roll over"],
    card_status: "pending",
    seconds_left: 540,
    expired: false,
    ...over,
  }) as SignalCard;

beforeEach(() => {
  vi.restoreAllMocks();
  useApp.setState({ conn: "open", status: status(), books: { [BOOK]: book() }, market: "crypto", segment: "futures", style: "intraday" });
});

describe("helpers", () => {
  it("rewardRisk divides reward by risk", () => {
    expect(rewardRisk("100", "90", "120")).toBe("2.00 : 1");
    expect(rewardRisk("100", "90", "115")).toBe("1.50 : 1");
  });
  it("rewardRisk is blank without a target or risk", () => {
    expect(rewardRisk("100", "90", null)).toBeNull();
    expect(rewardRisk("100", "100", "120")).toBeNull();
    expect(rewardRisk("abc", "90", "120")).toBeNull();
  });
  it("expiresIn reads naturally", () => {
    expect(expiresIn(540)).toBe("9m left");
    expect(expiresIn(30)).toBe("30s left");
    expect(expiresIn(0)).toBe("expired");
    expect(expiresIn(-5)).toBe("expired");
    expect(expiresIn(null)).toBe("no expiry");
  });
  it("filterParams maps each choice to query params", () => {
    expect(filterParams("all")).toEqual({});
    expect(filterParams("take")).toEqual({ decision: "take" });
    expect(filterParams("skip")).toEqual({ decision: "skip" });
    expect(filterParams("executed")).toEqual({ executed: "true" });
  });
  it("label makes config keys readable", () => {
    expect(label("max_risk_per_trade_pct")).toBe("max risk per trade pct");
  });
});

describe("LLM quota in the top bar", () => {
  it("shows today's calls against the budget", () => {
    render(<LlmQuota />);
    const el = screen.getByTestId("llm-quota");
    expect(el).toHaveTextContent("LLM 7/200");
    expect(el).toHaveAttribute("aria-label", "LLM calls today: 7 of 200");
  });

  it("shows a dash when there is no analyst", () => {
    useApp.setState({ status: status({ llm: llm({ enabled: false, daily_budget: null, calls_today: 0, note: "llm off" }) }) });
    render(<LlmQuota />);
    expect(screen.getByTestId("llm-quota")).toHaveTextContent("LLM 0/—");
  });

  it("warns when the budget is used up", () => {
    useApp.setState({ status: status({ llm: llm({ exhausted: true, calls_today: 200, remaining: 0 }) }) });
    render(<LlmQuota />);
    expect(screen.getByTestId("llm-quota").className).toContain("text-warning");
  });
});

describe("mode switching", () => {
  it("enables Co-pilot and Auto once the analyst is running", () => {
    render(<TopBar />);
    expect(screen.getByRole("radio", { name: "Co-pilot" })).toBeEnabled();
    expect(screen.getByRole("radio", { name: "Auto" })).toBeEnabled();
  });

  it("disables the agent modes when there is no analyst", () => {
    useApp.setState({ status: status({ llm: llm({ enabled: false, note: "GEMINI_API_KEY is not set" }) }) });
    render(<TopBar />);
    expect(screen.getByRole("radio", { name: "Co-pilot" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Auto" })).toBeDisabled();
  });

  it("switches to Co-pilot without a confirmation", async () => {
    const spy = vi.spyOn(api, "setMode").mockResolvedValue({ mode: "copilot" });
    render(<TopBar />);
    await userEvent.click(screen.getByRole("radio", { name: "Co-pilot" }));
    expect(spy).toHaveBeenCalledWith(BOOK, "copilot");
  });

  it("requires a confirmation before Auto", async () => {
    const spy = vi.spyOn(api, "setMode").mockResolvedValue({ mode: "auto" });
    render(<TopBar />);
    await userEvent.click(screen.getByRole("radio", { name: "Auto" }));
    expect(spy).not.toHaveBeenCalled();
    expect(screen.getByText(/Switch to Auto mode\?/)).toBeInTheDocument();
    expect(screen.getByText(/places paper trades by itself/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Yes, let the agent trade/ }));
    expect(spy).toHaveBeenCalledWith(BOOK, "auto", true);
  });

  it("cancelling the dialog leaves the mode alone", async () => {
    const spy = vi.spyOn(api, "setMode").mockResolvedValue({ mode: "auto" });
    render(<TopBar />);
    await userEvent.click(screen.getByRole("radio", { name: "Auto" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("SignalCardFeed", () => {
  it("shows the setup, levels, confidence and the plain-English why", async () => {
    vi.spyOn(api, "cards").mockResolvedValue([card()]);
    render(<SignalCardFeed bookId={BOOK} mode="copilot" />);
    await waitFor(() => expect(screen.getByTestId("signal-cards")).toBeInTheDocument());
    const el = screen.getByTestId("card-sig_1");
    expect(el).toHaveTextContent("LONG");
    expect(el).toHaveTextContent("ema_pullback");
    expect(el).toHaveTextContent("confidence 88");
    expect(el).toHaveTextContent("pullback to EMA21");
    expect(el).toHaveTextContent("R:R 2.00 : 1");
    expect(el).toHaveTextContent("9m left");
    expect(screen.getByTestId("why-sig_1")).toHaveTextContent("Trend intact");
    expect(el).toHaveTextContent("ADX may roll over");
  });

  it("approves a card", async () => {
    vi.spyOn(api, "cards").mockResolvedValue([card()]);
    const spy = vi.spyOn(api, "resolveCard").mockResolvedValue({ card_status: "approved", trade_id: "t1" });
    render(<SignalCardFeed bookId={BOOK} mode="copilot" />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Approve/ })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Approve/ }));
    expect(spy).toHaveBeenCalledWith("sig_1", true);
    expect(await screen.findByTestId("card-result")).toHaveTextContent("Approved");
  });

  it("rejects a card and says it is shadow-tracked", async () => {
    vi.spyOn(api, "cards").mockResolvedValue([card()]);
    const spy = vi.spyOn(api, "resolveCard").mockResolvedValue({ card_status: "rejected", shadow_trade_id: "t2" });
    render(<SignalCardFeed bookId={BOOK} mode="copilot" />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Reject/ })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Reject/ }));
    expect(spy).toHaveBeenCalledWith("sig_1", false);
    expect(await screen.findByTestId("card-result")).toHaveTextContent("shadow book");
  });

  it("says when a risk rule blocked an approved card", async () => {
    vi.spyOn(api, "cards").mockResolvedValue([card()]);
    vi.spyOn(api, "resolveCard").mockResolvedValue({ card_status: "approved", trade_id: null });
    render(<SignalCardFeed bookId={BOOK} mode="copilot" />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Approve/ })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /Approve/ }));
    expect(await screen.findByTestId("card-result")).toHaveTextContent("blocked by a risk rule");
  });

  it("cannot approve an expired card", async () => {
    vi.spyOn(api, "cards").mockResolvedValue([card({ seconds_left: 0, expired: true })]);
    render(<SignalCardFeed bookId={BOOK} mode="copilot" />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Approve/ })).toBeDisabled());
    expect(screen.getByTestId("card-sig_1")).toHaveTextContent("expired");
  });

  it("hides the buttons outside Co-pilot", async () => {
    vi.spyOn(api, "cards").mockResolvedValue([card()]);
    render(<SignalCardFeed bookId={BOOK} mode="auto" />);
    await waitFor(() => expect(screen.getByTestId("card-sig_1")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.getByText(/the agent places these itself/)).toBeInTheDocument();
  });

  it("says so when nothing is waiting", async () => {
    vi.spyOn(api, "cards").mockResolvedValue([]);
    render(<SignalCardFeed bookId={BOOK} mode="copilot" />);
    await waitFor(() => expect(screen.getByText("No cards waiting.")).toBeInTheDocument());
  });
});

describe("Journal", () => {
  const rows: SignalRow[] = [
    {
      id: "sig_take",
      ts: "2026-10-01T10:00:00Z",
      strategy_id: "ema_pullback",
      direction: "long",
      entry_ref: "65000",
      stop_loss: "64000",
      target: "67000",
      setup: "pullback",
      decision: "take",
      executed: true,
      risk_rule_blocked: null,
      regime: { label: "trending_up" },
      confidence: 88,
      reasoning: "Trend intact.",
      card_status: "approved",
      prompt_version: "analyst_v1",
      decision_by: "llm",
    },
    {
      id: "sig_skip",
      ts: "2026-10-01T09:00:00Z",
      strategy_id: "supertrend_flip",
      direction: "short",
      entry_ref: "66000",
      stop_loss: "67000",
      target: null,
      setup: "flip",
      decision: "skip",
      executed: false,
      risk_rule_blocked: null,
      regime: { label: "ranging" },
      confidence: 40,
      reasoning: "Confidence below threshold.",
      card_status: "none",
    },
  ];

  it("lists taken and skipped signals with the reasoning", async () => {
    vi.spyOn(api, "journal").mockResolvedValue(rows);
    render(<Journal />);
    const table = await screen.findByTestId("journal-table");
    expect(table).toHaveTextContent("TAKE");
    expect(table).toHaveTextContent("SKIP");
    expect(table).toHaveTextContent("Trend intact.");
    expect(table).toHaveTextContent("Confidence below threshold.");
    expect(table).toHaveTextContent("approved");
  });

  it("filters to skipped signals", async () => {
    const spy = vi.spyOn(api, "journal").mockResolvedValue(rows);
    render(<Journal />);
    await screen.findByTestId("journal-table");
    await userEvent.click(screen.getByRole("radio", { name: "Skipped" }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith(BOOK, { decision: "skip" }, 200));
  });

  it("filters by strategy", async () => {
    const spy = vi.spyOn(api, "journal").mockResolvedValue(rows);
    render(<Journal />);
    await screen.findByTestId("journal-table");
    await userEvent.selectOptions(screen.getByLabelText("Strategy"), "supertrend_flip");
    await waitFor(() => expect(spy).toHaveBeenCalledWith(BOOK, { strategy_id: "supertrend_flip" }, 200));
  });

  it("opens a detail drawer with the LLM call and every shadow outcome", async () => {
    vi.spyOn(api, "journal").mockResolvedValue(rows);
    const detail: SignalDetail = {
      signal: rows[0]!,
      llm_call: {
        id: "llm_1",
        ts: "2026-10-01T10:00:00Z",
        book_id: BOOK,
        signal_id: "sig_take",
        purpose: "analyst",
        provider: "gemini",
        model: "gemini-3.8-flash",
        prompt_version: "analyst_v1",
        prompt_hash: "abc",
        system_prompt: "sys",
        prompt: "the prompt",
        response: '{"decision":"TAKE"}',
        ok: true,
        error_reason: null,
        error: null,
        latency_ms: 812,
        input_tokens: 900,
        output_tokens: 60,
        thought_tokens: 10,
        total_tokens: 970,
        cached: false,
      },
      trade: { status: "closed", avg_entry: "65013", avg_exit: "66000", net_pnl: "4.8", r_multiple: "0.95" } as never,
      baseline_trade: null,
      raw_trade: { status: "closed", avg_entry: "65013", avg_exit: "64000", net_pnl: "-5.2", r_multiple: "-1.0" } as never,
      rejected_trade: null,
    };
    const spy = vi.spyOn(api, "signalDetail").mockResolvedValue(detail);
    render(<Journal />);
    await screen.findByTestId("journal-table");
    await userEvent.click(screen.getAllByRole("button", { name: "Details" })[0]!);
    expect(spy).toHaveBeenCalledWith("sig_take");
    const drawer = await screen.findByTestId("signal-drawer");
    expect(drawer).toHaveTextContent("confidence 88");
    expect(drawer).toHaveTextContent("prompt analyst_v1");
    expect(drawer).toHaveTextContent("Agent");
    expect(drawer).toHaveTextContent("Unfiltered strategy");
    expect(drawer).toHaveTextContent("812ms");

    // the prompt is behind a toggle so the table stays readable
    expect(drawer).not.toHaveTextContent("the prompt");
    await userEvent.click(screen.getByRole("button", { name: /Show prompt and response/ }));
    expect(screen.getByTestId("signal-drawer")).toHaveTextContent("the prompt");
  });

  it("shows a failed LLM call as the reason for no trade", async () => {
    vi.spyOn(api, "journal").mockResolvedValue([rows[1]!]);
    vi.spyOn(api, "signalDetail").mockResolvedValue({
      signal: rows[1]!,
      llm_call: {
        id: "llm_2",
        ok: false,
        error_reason: "llm_timeout",
        error: "no response within 30s",
        provider: "gemini",
        model: "m",
        latency_ms: 30000,
        prompt: "",
        response: "",
        cached: false,
        total_tokens: null,
      } as never,
      trade: null,
      baseline_trade: null,
      raw_trade: null,
      rejected_trade: null,
    });
    render(<Journal />);
    await screen.findByTestId("journal-table");
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    const drawer = await screen.findByTestId("signal-drawer");
    expect(drawer).toHaveTextContent("FAILED (llm_timeout)");
    expect(drawer).toHaveTextContent("no response within 30s");
    expect(drawer).toHaveTextContent("No trade came from this signal.");
  });
});

describe("Settings", () => {
  const settings: SettingsView = {
    books: {
      [BOOK]: {
        id: BOOK,
        mode: "copilot",
        currency: "USDT",
        starting_capital: "1000",
        risk: { max_leverage: "3", max_risk_per_trade_pct: "1", max_open_positions: 2 },
        slippage: { model: "bps", bps: "2" },
        copilot: { card_ttl_bars: 1, shadow_track_rejected: true },
        strategies: [{ id: "ema_pullback", enabled: true, params: { adx_min: 20 } }],
        charges: { name: "crypto", taker_pct: "0.05" },
      },
    },
    llm: {
      provider: "gemini",
      model: "gemini-3.8-flash",
      confidence_threshold: 70,
      prompt_version: "analyst_v1",
      limits: { requests_per_minute: 10, requests_per_day: 200, tokens_per_minute: null },
    },
    llm_runtime: llm({ cached_hits: 3 }),
    data: { stale_after_seconds: 10, provider: "ccxt", exchange: "binanceusdm" },
    secrets: { gemini_api_key: true, angel_api_key: false },
  };

  it("shows the model, limits and today's usage", async () => {
    vi.spyOn(api, "settings").mockResolvedValue(settings);
    render(<Settings />);
    const block = await screen.findByTestId("llm-settings");
    expect(block).toHaveTextContent("gemini-3.8-flash");
    expect(block).toHaveTextContent("7 / 200 calls");
    expect(block).toHaveTextContent("+3 from cache");
    expect(block).toHaveTextContent("requests per day");
    expect(block).toHaveTextContent("not tracked"); // tokens_per_minute is null
  });

  it("shows API keys as set or not set, never the value", async () => {
    vi.spyOn(api, "settings").mockResolvedValue(settings);
    const { container } = render(<Settings />);
    const secrets = await screen.findByTestId("secrets");
    expect(secrets).toHaveTextContent("gemini_api_key");
    expect(secrets).toHaveTextContent("set");
    expect(secrets).toHaveTextContent("not set");
    expect(container.textContent).not.toContain("sk-");
  });

  it("shows the book's risk rules and strategies read-only", async () => {
    vi.spyOn(api, "settings").mockResolvedValue(settings);
    render(<Settings />);
    await screen.findByTestId("settings-page");
    expect(screen.getByText(/Read-only/)).toBeInTheDocument();
    expect(screen.getByText("max risk per trade pct")).toBeInTheDocument();
    expect(screen.getByTestId(`strategies-${BOOK}`)).toHaveTextContent("ema_pullback");
    expect(screen.getByTestId(`strategies-${BOOK}`)).toHaveTextContent("adx_min=20");
  });
});
