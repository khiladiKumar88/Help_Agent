import type {
  ActorPnl,
  BookSummary,
  Candle,
  EquityPoint,
  Instrument,
  OrderForm,
  Preview,
  RiskDecision,
  RiskDecisionRow,
  SystemStatus,
  TradeView,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public ruleId?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { "content-type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, "Backend unreachable");
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      throw new ApiError(res.status, detail.message ?? "Request failed", detail.rule_id ?? undefined);
    }
    const msg = Array.isArray(detail) ? detail.map((d: { msg: string }) => d.msg).join("; ") : detail;
    throw new ApiError(res.status, msg ?? `HTTP ${res.status}`);
  }
  return body as T;
}

const json = (method: string, data?: unknown): RequestInit => ({
  method,
  body: data === undefined ? undefined : JSON.stringify(data),
});

export const api = {
  status: () => request<SystemStatus>("/api/system/status"),
  book: (id: string) => request<BookSummary>(`/api/books/${id}`),
  setMode: (id: string, mode: string) => request<{ mode: string }>(`/api/books/${id}/mode`, json("PUT", { mode })),
  trades: (id: string, status?: string) =>
    request<TradeView[]>(`/api/books/${id}/trades${status ? `?status=${status}` : ""}`),
  equity: (id: string) => request<EquityPoint[]>(`/api/books/${id}/equity`),
  actorPnl: (id: string) => request<ActorPnl>(`/api/books/${id}/actor-pnl`),
  riskDecisions: (id: string) => request<RiskDecisionRow[]>(`/api/books/${id}/risk-decisions?limit=20`),
  instruments: (bookId: string) => request<Instrument[]>(`/api/market/instruments?book_id=${bookId}`),
  candles: (instrumentId: string, tf = "1m", limit = 300) =>
    request<{ closed: Candle[]; forming: Candle | null }>(
      `/api/market/candles?instrument_id=${encodeURIComponent(instrumentId)}&tf=${tf}&limit=${limit}`,
    ),
  preview: (o: OrderForm) => request<Preview>("/api/orders/preview", json("POST", o)),
  place: (o: OrderForm) =>
    request<{ approved: boolean; decision: RiskDecision; trade: TradeView | null }>("/api/orders", json("POST", o)),
  closeTrade: (id: string) => request<TradeView>(`/api/trades/${id}/close`, json("POST")),
  cancelTrade: (id: string) => request<TradeView>(`/api/trades/${id}/cancel`, json("POST")),
  modifyTrade: (id: string, body: { stop_loss?: string; target?: string; clear_target?: boolean }) =>
    request<TradeView>(`/api/trades/${id}`, json("PATCH", body)),
  killSwitch: (reason: string) =>
    request<{ engaged: boolean; closed: number; cancelled: number }>(
      "/api/system/kill-switch",
      json("POST", { confirm: true, reason }),
    ),
  releaseKillSwitch: () => request<{ engaged: boolean }>("/api/system/kill-switch/release", json("POST")),
};
