// API shapes. Money/prices arrive as decimal strings (exact); convert only for display.

export type Market = "crypto" | "india";
export type Segment = "spot" | "futures" | "options";
export type Style = "intraday" | "swing";
export type Mode = "manual" | "copilot" | "auto";
export type Direction = "long" | "short";
export type AgentStatus = "running" | "thinking" | "rate_limited" | "market_closed" | "halted" | "stale" | "disconnected";

export interface Tick {
  instrument_id: string;
  ts: string;
  ltp: string;
  bid: string | null;
  ask: string | null;
  volume: string | null;
  source: string;
}

export interface Candle {
  instrument_id: string;
  timeframe: string;
  ts_open: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  closed: boolean;
}

export interface Instrument {
  id: string;
  market: Market;
  segment: Segment;
  exchange: string;
  symbol: string;
  underlying: string;
  kind: string;
  quote_ccy: string;
  settle_ccy: string;
  contract_size: string;
  lot_size: string;
  tick_size: string;
  qty_step: string;
  min_qty: string;
}

export interface TradeView {
  id: string;
  book_id: string;
  account: string;
  actor: "human" | "agent" | "baseline";
  instrument_id: string;
  symbol: string;
  direction: Direction;
  status: "pending" | "open" | "closed" | "cancelled";
  qty: string;
  leverage: string;
  avg_entry: string | null;
  avg_exit: string | null;
  initial_sl: string;
  current_sl: string;
  target: string | null;
  initial_risk: string | null;
  gross_pnl: string;
  charges: string;
  funding: string;
  net_pnl: string;
  r_multiple: string | null;
  exit_reason: string | null;
  stale_exit: boolean;
  created_at: string;
  opened_at: string | null;
  closed_at: string | null;
  mark_price?: string;
  unrealized_gross?: string;
  est_exit_charges?: string;
  unrealized_net?: string;
  unrealized_r?: string | null;
  stale?: boolean;
}

export interface BookSummary {
  book_id: string;
  currency: string;
  mode: Mode;
  market: Market;
  segment: Segment;
  style: Style;
  halted: boolean;
  halt_reason: string | null;
  halted_until: string | null;
  kill_switch: boolean;
  starting_capital: string;
  balance: string;
  unrealized: string;
  est_exit_charges: string;
  equity: string;
  used_margin: string;
  available_margin: string;
  today_pnl: string;
  total_pnl: string;
  positions: TradeView[];
  instruments: string[];
}

export interface RuleResult {
  rule_id: string;
  passed: boolean;
  message: string;
  metrics: Record<string, string>;
}

export interface RiskDecision {
  approved: boolean;
  failed_rule_id: string | null;
  message: string;
  results: RuleResult[];
  risk_amount: string | null;
  est_charges: string | null;
}

export interface RiskDecisionRow {
  id: string;
  ts: string;
  actor: string;
  instrument_id: string;
  approved: boolean;
  failed_rule_id: string | null;
  message: string;
}

export interface Preview {
  decision: RiskDecision;
  est_entry: string | null;
  notional: string | null;
  margin_required: string | null;
  available_margin: string;
  risk_amount: string | null;
  reward_amount: string | null;
  reward_risk: string | null;
  est_round_trip_charges: string;
  max_risk_allowed: string | null;
  max_qty_by_risk: string;
  stale: boolean;
}

export interface SystemStatus {
  server_time: string;
  replay: boolean;
  kill_switch: { engaged: boolean; at?: string; reason?: string };
  provider: { provider: string; exchange: string; connected: boolean; feed_mode?: string; last_error?: string | null };
  data: Record<string, { stale: boolean; age_seconds: number | null }>;
  stale_after_seconds: number;
  books: Record<string, { status: AgentStatus; mode: Mode }>;
  llm: { provider: string | null; calls_today: number; daily_budget: number | null; note?: string };
  secrets: Record<string, boolean>;
}

export interface EquityPoint {
  ts: string;
  balance: string;
  unrealized: string;
  equity: string;
}

export interface ActorPnl {
  series: Record<string, { ts: string; cum_net_pnl: string; r: string | null }[]>;
  totals: Record<string, string>;
}

export interface OrderForm {
  book_id: string;
  instrument_id: string;
  direction: Direction;
  qty: string;
  order_type: "market" | "limit";
  limit_price?: string;
  stop_loss?: string;
  target?: string;
  leverage: string;
}
