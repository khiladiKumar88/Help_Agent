import { render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { SignalsCard } from "@/components/SignalsCard";
import { api } from "@/lib/api";
import { cardNote, signalVerdict } from "@/lib/signalVerdict";
import type { LLMStatus, SignalRow } from "@/lib/types";
import { useApp } from "@/store/app";

const signal = (over: Partial<SignalRow> = {}): SignalRow =>
  ({
    id: "s1",
    ts: "2026-03-01T00:15:00Z",
    strategy_id: "supertrend_flip",
    direction: "short",
    entry_ref: "65000",
    stop_loss: "65500",
    target: "64000",
    setup: "Supertrend flipped down",
    decision: "take",
    executed: false,
    risk_rule_blocked: null,
    regime: { label: "trending_down" },
    ...over,
  }) as SignalRow;

const llmOff = (note: string): LLMStatus =>
  ({ provider: null, model: "gemini-3.8-flash", calls_today: 0, daily_budget: null, enabled: false, note }) as LLMStatus;

describe("signalVerdict", () => {
  it("reports the analyst's TAKE with its confidence and reason", () => {
    const v = signalVerdict(signal({ decision_by: "llm", confidence: 82, reasoning: "clean break, volume confirms" }));
    expect(v).toMatchObject({ label: "LLM TAKE", tone: "profit", confidence: 82, sentToLlm: true });
    expect(v.detail).toBe("clean break, volume confirms");
  });

  it("reports the analyst's SKIP", () => {
    const v = signalVerdict(signal({ decision: "skip", decision_by: "llm", confidence: 40, reasoning: "confidence 40 is below the 70 threshold" }));
    expect(v).toMatchObject({ label: "LLM SKIP", tone: "loss", confidence: 40, sentToLlm: true });
  });

  it("says the LLM was never asked, and quotes the backend's reason", () => {
    const s = signal({ decision_by: "rules", decision_reason: "strategy signal (rules-only agent)" });
    const v = signalVerdict(s, llmOff("GEMINI_API_KEY is not set — add it to .env"));
    expect(v).toMatchObject({ label: "not sent to LLM", tone: "warning", sentToLlm: false });
    expect(v.detail).toBe("GEMINI_API_KEY is not set — add it to .env");
  });

  it("falls back to the signal's own reason when the LLM status is unknown", () => {
    const v = signalVerdict(signal({ decision_by: "rules", decision_reason: "rules-only agent" }));
    expect(v.detail).toBe("rules-only agent");
  });

  it("surfaces a paused analyst as the reason nothing was sent", () => {
    const llm = { ...llmOff(""), enabled: true, paused: true, pause_reason: "daily budget of 200 calls used up" } as LLMStatus;
    const v = signalVerdict(signal({ decision_by: "rules" }), llm);
    expect(v.detail).toBe("daily budget of 200 calls used up");
  });

  it("names a decider crash rather than blaming the LLM", () => {
    const v = signalVerdict(signal({ decision: "skip", decision_by: "error", decision_reason: "decider error: boom" }));
    expect(v.label).toBe("not sent to LLM");
    expect(v.detail).toBe("decider error: boom");
  });

  it("marks a signal with no verdict yet", () => {
    expect(signalVerdict(signal({ decision: "" }))).toMatchObject({ label: "deciding…", tone: "outline" });
  });
});

describe("cardNote", () => {
  it("explains why a TAKE produced no card in Manual mode", () => {
    expect(cardNote(signal({ card_status: "none" }), "manual")).toBe("no card — Manual mode records signals only");
  });

  it("shows the card's state once there is one", () => {
    expect(cardNote(signal({ card_status: "pending" }), "copilot")).toBe("card pending");
    expect(cardNote(signal({ card_status: "expired" }), "copilot")).toBe("card expired");
  });

  it("says what Auto did with it", () => {
    expect(cardNote(signal({ executed: true }), "auto")).toBe("placed by the agent");
    expect(cardNote(signal({ risk_rule_blocked: "R004_STALE_DATA" }), "auto")).toBe("not placed — R004_STALE_DATA");
  });

  it("stays quiet about a skipped signal", () => {
    expect(cardNote(signal({ decision: "skip" }), "copilot")).toBeNull();
  });
});

describe("SignalsCard shows the decision next to each signal", () => {
  beforeEach(() => useApp.setState({ conn: "open" }));

  it("shows the LLM verdict and confidence", async () => {
    vi.spyOn(api, "signals").mockResolvedValue([
      signal({ decision_by: "llm", confidence: 82, reasoning: "momentum intact", card_status: "pending" }),
    ]);
    render(<SignalsCard bookId="crypto-futures-intraday" mode="copilot" />);
    const row = await screen.findByTestId("verdict-s1");
    expect(row).toHaveTextContent("LLM TAKE");
    expect(row).toHaveTextContent("confidence 82");
    expect(row).toHaveTextContent("card pending");
    expect(screen.getByTestId("signals")).toHaveTextContent("momentum intact");
  });

  it("says the analyst was not called, and why, without opening the Journal", async () => {
    vi.spyOn(api, "signals").mockResolvedValue([signal({ decision_by: "rules", card_status: "none" })]);
    render(
      <SignalsCard
        bookId="crypto-futures-intraday"
        mode="manual"
        llm={llmOff("GEMINI_API_KEY is not set — add it to .env to enable the Gemini analyst")}
      />,
    );
    expect(await screen.findByTestId("verdict-s1")).toHaveTextContent("not sent to LLM");
    expect(screen.getByTestId("signals")).toHaveTextContent("GEMINI_API_KEY is not set");
    expect(screen.getByTestId("signals")).toHaveTextContent("no card — Manual mode records signals only");
  });
});
