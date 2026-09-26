import { create } from "zustand";
import type { BookSummary, Candle, Market, RiskDecisionRow, Segment, Style, SystemStatus, Tick, TradeView } from "@/lib/types";
import { bookId } from "@/lib/utils";

export type ConnState = "connecting" | "open" | "closed";

export interface TickEntry {
  tick: Tick;
  receivedAt: number; // client ms
}

interface AppState {
  conn: ConnState;
  lastMessageAt: number | null;
  reconnectInMs: number | null;
  status: SystemStatus | null;
  market: Market;
  segment: Segment;
  style: Style;
  books: Record<string, BookSummary>;
  ticks: Record<string, TickEntry>;
  lastCandle: Record<string, Candle>;
  riskFeed: RiskDecisionRow[];
  tradeVersion: number; // bumps when any trade changes (triggers refetch of history charts)
  setConn: (c: ConnState, reconnectInMs?: number | null) => void;
  setSelection: (p: Partial<Pick<AppState, "market" | "segment" | "style">>) => void;
  setStatus: (s: SystemStatus) => void;
  setBook: (b: BookSummary) => void;
  onTick: (t: Tick) => void;
  onCandle: (c: Candle) => void;
  onTrade: (t: TradeView) => void;
  setRiskFeed: (rows: RiskDecisionRow[]) => void;
  pushRisk: (row: RiskDecisionRow) => void;
  touch: () => void;
}

export const useApp = create<AppState>((set) => ({
  conn: "connecting",
  lastMessageAt: null,
  reconnectInMs: null,
  status: null,
  market: "crypto",
  segment: "futures",
  style: "intraday",
  books: {},
  ticks: {},
  lastCandle: {},
  riskFeed: [],
  tradeVersion: 0,
  setConn: (conn, reconnectInMs = null) => set({ conn, reconnectInMs }),
  setSelection: (p) => set(p),
  setStatus: (status) => set({ status }),
  setBook: (b) => set((s) => ({ books: { ...s.books, [b.book_id]: b } })),
  onTick: (t) => set((s) => ({ ticks: { ...s.ticks, [t.instrument_id]: { tick: t, receivedAt: Date.now() } } })),
  onCandle: (c) =>
    c.timeframe === "1m" ? set((s) => ({ lastCandle: { ...s.lastCandle, [c.instrument_id]: c } })) : undefined,
  onTrade: () => set((s) => ({ tradeVersion: s.tradeVersion + 1 })),
  setRiskFeed: (riskFeed) => set({ riskFeed }),
  pushRisk: (row) => set((s) => ({ riskFeed: [row, ...s.riskFeed].slice(0, 50) })),
  touch: () => set({ lastMessageAt: Date.now() }),
}));

export const selectBookId = (s: AppState) => bookId(s.market, s.segment, s.style);

/** A price is stale if the socket is down or the last tick is older than the server's threshold. */
export function isPriceStale(entry: TickEntry | undefined, conn: ConnState, staleAfter: number, now = Date.now()): boolean {
  if (!entry || conn !== "open") return true;
  return (now - entry.receivedAt) / 1000 > staleAfter;
}
