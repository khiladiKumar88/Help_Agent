/** What the analyst did with a signal, as one line you can read without opening the Journal.
 *
 * Every signal takes one of four routes, and the UI must be able to tell them apart — "signals
 * but no cards" is otherwise impossible to explain from the Live page:
 *   * sent to the LLM  -> TAKE/SKIP plus its confidence and reason
 *   * not sent         -> the LLM is off or unavailable, so the rules agent decided
 *   * decider crashed  -> NO TRADE, with the error
 *   * not decided yet  -> still in flight
 */
import type { CardStatus, LLMStatus, SignalRow } from "./types";

export type VerdictTone = "profit" | "loss" | "warning" | "outline";

export interface Verdict {
  /** Short badge text, e.g. "LLM TAKE", "not sent to LLM". */
  label: string;
  tone: VerdictTone;
  /** Confidence as the analyst reported it, or null when it never answered. */
  confidence: number | null;
  /** The reason: the analyst's own words, or why it was never asked. */
  detail: string;
  /** True when no LLM call was made for this signal. */
  sentToLlm: boolean;
}

const NOT_RUNNING = "the LLM analyst is not running";

/** Why no LLM call happened, preferring the backend's own explanation. */
function notSentReason(s: SignalRow, llm: LLMStatus | null): string {
  if (s.decision_by === "error") return s.decision_reason || "the decider failed";
  if (llm && !llm.enabled) return llm.note || NOT_RUNNING;
  if (llm?.paused) return llm.pause_reason || "the daily LLM budget is used up";
  return s.decision_reason || NOT_RUNNING;
}

export function signalVerdict(s: SignalRow, llm: LLMStatus | null = null): Verdict {
  const confidence = typeof s.confidence === "number" ? s.confidence : null;
  if (!s.decision) {
    return { label: "deciding…", tone: "outline", confidence, detail: "no verdict recorded yet", sentToLlm: false };
  }
  const took = s.decision === "take";
  if (s.decision_by === "llm") {
    return {
      label: took ? "LLM TAKE" : "LLM SKIP",
      tone: took ? "profit" : "loss",
      confidence,
      detail: s.reasoning || s.decision_reason || (took ? "analyst said TAKE" : "analyst said SKIP"),
      sentToLlm: true,
    };
  }
  if (s.decision_by === "human") {
    return {
      label: took ? "you approved" : "you rejected",
      tone: took ? "profit" : "loss",
      confidence,
      detail: s.decision_reason || "",
      sentToLlm: Boolean(s.llm_call_id),
    };
  }
  return {
    label: "not sent to LLM",
    tone: "warning",
    confidence,
    detail: notSentReason(s, llm),
    sentToLlm: false,
  };
}

/** Why a TAKE did or did not become a Co-pilot card — the "signals but no cards" question. */
export function cardNote(s: SignalRow, mode: string): string | null {
  const status: CardStatus | undefined = s.card_status;
  if (status && status !== "none") return `card ${status}`;
  if (s.decision !== "take") return null;
  if (mode === "manual") return "no card — Manual mode records signals only";
  if (mode === "auto") return s.executed ? "placed by the agent" : `not placed — ${s.risk_rule_blocked ?? "blocked by a risk rule"}`;
  return "no card was created";
}
