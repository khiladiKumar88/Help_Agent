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

/** What the book's slippage model assumes, so est_entry vs the real fill is explainable. */
export interface SlippagePreview {
  model: "bps" | "spread";
  bps: string;
  est_spread_bps: string;
  extra_ticks: number;
  reference_price: string;
  reference_kind: string; // ask | bid | ltp_half_spread
  est_fill: string;
  per_unit: string;
  cost: string;
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
  slippage: SlippagePreview | null;
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

// ---------------------------------------------------------------- Phase 2

export interface StrategyInfo {
  id: string;
  name: string;
  description: string;
  markets: string[];
  default_params: Record<string, number>;
  param_grid: Record<string, number[]>;
  grid_size: number;
}

export interface Coverage {
  exchange: string;
  symbol: string;
  timeframe: string;
  first: string | null;
  last: string | null;
  bars: number;
  expected_bars: number;
  missing_bars: number;
  complete_pct: number;
  gaps: { start: string; end: string; missing_bars: number }[];
}

export interface Dataset {
  exchange: string;
  symbol: string;
  timeframe: string;
  first: string;
  last: string;
  bars: number;
}

export interface MetricsRow {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  net_profit: string;
  fees: string;
  funding: string;
  avg_r: number | null;
  profit_factor: number | null;
  max_drawdown: string;
  max_drawdown_pct: number;
  return_pct: number;
  sharpe: number | null;
  best_trade: string;
  worst_trade: string;
  avg_hold_minutes: number | null;
}

export interface BuyHoldRow {
  net_profit: string;
  return_pct: number;
  fees: string;
  max_drawdown_pct: number;
}

export interface Verdict {
  meaningful: boolean;
  min_trades: number;
  beat_baseline: boolean | null;
  beat_buy_hold: boolean;
}

export interface BtTrade {
  id: string;
  actor: "agent" | "baseline" | "human";
  direction: "long" | "short";
  qty: string;
  entry: string | null;
  exit: string | null;
  stop: string;
  target: string | null;
  opened_at: string | null;
  closed_at: string | null;
  net_pnl: string;
  fees: string;
  r: string | null;
  exit_reason: string | null;
  note: string;
}

export interface FoldRow {
  fold: number;
  train: [string, string];
  test: [string, string];
  params: Record<string, number>;
  why: string;
  oos_trades: number;
  oos_net_profit: string;
  oos_avg_r: number | null;
  baseline_net_profit: string;
  baseline_avg_r: number | null;
}

export interface SignalRow {
  id: string;
  ts: string;
  strategy_id: string;
  direction: "long" | "short";
  entry_ref: string;
  stop_loss: string;
  target: string | null;
  setup: string;
  decision: string;
  executed: boolean;
  risk_rule_blocked: string | null;
  regime: string | Record<string, unknown> | null;
}

export interface BacktestResult {
  kind: "backtest" | "walkforward" | "replay";
  label: string;
  approximate: boolean;
  currency: string;
  instrument: string;
  period: { start: string; end: string };
  data: { bars: number; expected_bars: number };
  agent: MetricsRow;
  baseline: MetricsRow;
  buy_hold: BuyHoldRow | null;
  verdict: Verdict;
  summary: string;
  equity: { agent: { t: string; v: string }[]; baseline: { t: string; v: string }[] };
  trades: BtTrade[];
  signals: number;
  candles: { t: string; o: string; h: string; l: string; c: string }[];
  folds?: FoldRow[];
  grid_size?: number;
  strategies?: string[];
  signal_list?: SignalRow[];
}

export interface JobRow {
  id: string;
  kind: "backtest" | "walkforward" | "replay" | "download";
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress: number;
  message: string | null;
  spec: Record<string, unknown>;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  summary?: string | null;
  verdict?: Verdict | null;
  result?: (BacktestResult & { summary: string }) | { summary: string } | null;
}

export interface JobBody {
  kind: "backtest" | "walkforward" | "replay";
  book_id: string;
  exchange: string;
  symbol: string;
  base_tf: string;
  strategy_id?: string;
  start?: string;
  end?: string;
  day?: string;
  train_days?: number;
  test_days?: number;
  min_trades?: number;
  speed?: number;
}
