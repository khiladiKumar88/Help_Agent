import { CircleAlert, CircleCheck, CircleX } from "lucide-react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { BacktestResult, MetricsRow } from "@/lib/types";
import { ACTOR_COLORS } from "@/lib/seriesColors";
import { fmtAxisTime, fmtDateTime, fmtNum, fmtSigned, pnlClass, timeTicks } from "@/lib/utils";
import { ReplayChart } from "./ReplayChart";

const SERIES = ACTOR_COLORS;
const TITLE = { backtest: "Backtest result", walkforward: "Walk-forward result (out-of-sample only)", replay: "Replay result" } as const;

function Verdict({ ok, yes, no }: { ok: boolean | null; yes: string; no: string }) {
  if (ok === null) return null;
  return (
    <Badge variant={ok ? "profit" : "loss"}>
      {ok ? <CircleCheck className="h-3.5 w-3.5" /> : <CircleX className="h-3.5 w-3.5" />}
      {ok ? yes : no}
    </Badge>
  );
}

export function ResultView({ r }: { r: BacktestResult }) {
  const v = r.verdict;
  return (
    <div className="space-y-4" data-testid="result-view">
      {r.approximate && (
        <div className="flex items-center gap-2 rounded-md bg-warning/15 px-4 py-2 text-sm text-warning">
          <CircleAlert className="h-4 w-4" /> APPROXIMATE — option prices were synthesized with Black-Scholes, not real quotes.
        </div>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-foreground">{TITLE[r.kind]}</CardTitle>
          <span className="text-xs text-muted-foreground">
            {fmtDateTime(r.period.start)} → {fmtDateTime(r.period.end)} · {r.instrument}
          </span>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm leading-relaxed" data-testid="plain-english">
            {r.summary}
          </p>
          <div className="flex flex-wrap gap-2">
            <Verdict ok={v.beat_baseline} yes="Beat the random baseline" no="Did NOT beat the random baseline" />
            <Verdict ok={v.beat_buy_hold} yes="Beat buy-and-hold" no="Did NOT beat buy-and-hold" />
            <Badge variant={v.meaningful ? "info" : "warning"}>
              {v.meaningful ? `≥ ${v.min_trades} trades: meaningful` : `< ${v.min_trades} trades: NOT meaningful yet`}
            </Badge>
            <Badge variant="outline">fees + slippage always on</Badge>
            <Badge variant="outline">
              data {r.data.bars}/{r.data.expected_bars} bars
            </Badge>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <MetricsTable r={r} />
        <PnlChart r={r} />
      </div>

      {r.kind === "replay" && r.candles.length > 0 && <ReplayChart r={r} />}
      {r.folds && <Folds r={r} />}
      {r.signal_list && r.signal_list.length > 0 && <SignalTable r={r} />}
      <Trades r={r} />
    </div>
  );
}

function MetricsTable({ r }: { r: BacktestResult }) {
  const rows: { label: string; get: (m: MetricsRow) => React.ReactNode; bh?: React.ReactNode }[] = [
    { label: `Net profit after fees (${r.currency})`, get: (m) => <span className={pnlClass(m.net_profit)}>{fmtSigned(m.net_profit)}</span>, bh: r.buy_hold ? <span className={pnlClass(r.buy_hold.net_profit)}>{fmtSigned(r.buy_hold.net_profit)}</span> : "—" },
    { label: "Return", get: (m) => `${fmtSigned(m.return_pct, 1)}%`, bh: r.buy_hold ? `${fmtSigned(r.buy_hold.return_pct, 1)}%` : "—" },
    { label: "Trades", get: (m) => m.trades, bh: "1" },
    { label: "Win rate", get: (m) => (m.trades ? `${fmtNum(m.win_rate * 100, 0)}%` : "—"), bh: "—" },
    { label: "Average R per trade", get: (m) => (m.avg_r === null ? "—" : `${fmtSigned(m.avg_r)}R`), bh: "—" },
    { label: "Profit factor", get: (m) => (m.profit_factor === null ? "—" : fmtNum(m.profit_factor, 2)), bh: "—" },
    { label: "Max drawdown", get: (m) => `${fmtNum(m.max_drawdown_pct, 1)}%`, bh: r.buy_hold ? `${fmtNum(r.buy_hold.max_drawdown_pct, 1)}%` : "—" },
    { label: `Fees paid (${r.currency})`, get: (m) => fmtNum(m.fees, 2), bh: r.buy_hold ? fmtNum(r.buy_hold.fees, 2) : "—" },
    { label: "Sharpe (annualized)", get: (m) => (m.sharpe === null ? "—" : fmtNum(m.sharpe, 2)), bh: "—" },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">
          {r.kind === "walkforward" ? "Out-of-sample results" : "Results"}
        </CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums" data-testid="metrics-table">
          <thead className="text-xs text-muted-foreground">
            <tr className="[&>th]:px-2 [&>th]:py-1 [&>th]:text-right [&>th:first-child]:text-left">
              <th />
              <th>
                <span className="mr-1 inline-block h-0.5 w-3 align-middle" style={{ background: SERIES.agent }} />
                Agent
              </th>
              <th>
                <span className="mr-1 inline-block h-0.5 w-3 align-middle" style={{ background: SERIES.baseline }} />
                Random baseline
              </th>
              <th>Buy &amp; hold</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label} className="border-t [&>td]:px-2 [&>td]:py-1.5 [&>td]:text-right [&>td:first-child]:text-left">
                <td className="text-muted-foreground">{row.label}</td>
                <td>{row.get(r.agent)}</td>
                <td>{row.get(r.baseline)}</td>
                <td>{row.bh}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function PnlChart({ r }: { r: BacktestResult }) {
  const events = [
    ...r.equity.agent.map((p) => ({ t: Date.parse(p.t), k: "agent" as const, v: Number(p.v) })),
    ...r.equity.baseline.map((p) => ({ t: Date.parse(p.t), k: "baseline" as const, v: Number(p.v) })),
  ].sort((a, b) => a.t - b.t);
  // one row per timestamp (trades can close at the same instant) carrying both series forward
  const cur: { agent?: number; baseline?: number } = {};
  const data: { t: number; agent?: number; baseline?: number }[] = [];
  for (const e of events) {
    cur[e.k] = e.v;
    const last = data[data.length - 1];
    if (last && last.t === e.t) Object.assign(last, cur);
    else data.push({ t: e.t, ...cur });
  }
  const ts = data.map((d) => d.t);
  const span = ts.length ? Math.max(...ts) - Math.min(...ts) : 0;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Cumulative net P&amp;L ({r.currency})</CardTitle>
        <span className="text-xs text-muted-foreground">after all fees, at each trade close</span>
      </CardHeader>
      <CardContent className="h-64">
        {data.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">No closed trades.</div>
        ) : (
          <ResponsiveContainer>
            <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke="rgba(255,255,255,0.06)" />
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} ticks={timeTicks(ts, 6)} tickFormatter={(t: number) => fmtAxisTime(t, span)} stroke="var(--muted-foreground)" fontSize={11} tickLine={false} axisLine={false} />
              <YAxis width={56} tickFormatter={(v: number) => fmtNum(v, 0)} stroke="var(--muted-foreground)" fontSize={11} tickLine={false} axisLine={false} />
              <Tooltip
                contentStyle={{ background: "var(--popover)", border: "1px solid var(--border)", fontSize: 12 }}
                labelFormatter={(t) => fmtDateTime(new Date(Number(t)).toISOString())}
                formatter={(v, name) => [fmtSigned(Number(v)), String(name)]}
              />
              <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />
              {/* baseline dashed and drawn first so the agent stays visible where they overlap */}
              <Line type="stepAfter" dataKey="baseline" name="Random baseline" stroke={SERIES.baseline} strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls isAnimationActive={false} />
              <Line type="stepAfter" dataKey="agent" name="Agent" stroke={SERIES.agent} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}

function Folds({ r }: { r: BacktestResult }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Walk-forward folds</CardTitle>
        <span className="text-xs text-muted-foreground">
          params picked on TRAIN only (grid of {r.grid_size}); numbers shown are the unseen TEST window
        </span>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-xs text-muted-foreground">
            <tr className="[&>th]:px-2 [&>th]:py-1 [&>th]:text-left">
              <th>#</th>
              <th>Train</th>
              <th>Test (out-of-sample)</th>
              <th>Chosen params</th>
              <th className="text-right">Trades</th>
              <th className="text-right">Agent P&amp;L</th>
              <th className="text-right">Avg R</th>
              <th className="text-right">Baseline P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {r.folds!.map((f) => (
              <tr key={f.fold} className="border-t [&>td]:px-2 [&>td]:py-1.5" title={f.why}>
                <td>{f.fold}</td>
                <td className="text-muted-foreground">
                  {f.train[0].slice(0, 10)} → {f.train[1].slice(0, 10)}
                </td>
                <td>
                  {f.test[0].slice(0, 10)} → {f.test[1].slice(0, 10)}
                </td>
                <td className="font-mono text-xs">{Object.entries(f.params).map(([k, v]) => `${k}=${v}`).join(" ")}</td>
                <td className="text-right">{f.oos_trades}</td>
                <td className={`text-right ${pnlClass(f.oos_net_profit)}`}>{fmtSigned(f.oos_net_profit)}</td>
                <td className="text-right">{f.oos_avg_r === null ? "—" : `${fmtSigned(f.oos_avg_r)}R`}</td>
                <td className={`text-right ${pnlClass(f.baseline_net_profit)}`}>{fmtSigned(f.baseline_net_profit)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function SignalTable({ r }: { r: BacktestResult }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Signals ({r.signal_list!.length})</CardTitle>
      </CardHeader>
      <CardContent className="max-h-72 overflow-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-xs text-muted-foreground">
            <tr className="[&>th]:px-2 [&>th]:py-1 [&>th]:text-left">
              <th>Time</th>
              <th>Strategy</th>
              <th>Side</th>
              <th>Regime</th>
              <th>Setup</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {r.signal_list!.map((s) => (
              <tr key={s.id} className="border-t [&>td]:px-2 [&>td]:py-1.5 align-top">
                <td className="whitespace-nowrap">{fmtDateTime(s.ts)}</td>
                <td>{s.strategy_id}</td>
                <td>{s.direction}</td>
                <td className="text-muted-foreground">{typeof s.regime === "string" ? s.regime : "—"}</td>
                <td className="max-w-md text-muted-foreground">{s.setup}</td>
                <td>{s.executed ? <Badge variant="profit">traded</Badge> : <Badge variant="outline">{s.risk_rule_blocked ?? s.decision}</Badge>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function Trades({ r }: { r: BacktestResult }) {
  const rows = r.trades.slice(0, 300);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Trades ({r.trades.length})</CardTitle>
      </CardHeader>
      <CardContent className="max-h-96 overflow-auto">
        <table className="w-full text-sm tabular-nums" data-testid="bt-trades">
          <thead className="sticky top-0 bg-card text-xs text-muted-foreground">
            <tr className="[&>th]:px-2 [&>th]:py-1 [&>th]:text-right [&>th:first-child]:text-left [&>th:nth-child(2)]:text-left">
              <th>Opened</th>
              <th>Who</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Exit reason</th>
              <th>Fees</th>
              <th>Net P&amp;L</th>
              <th>R</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id} className="border-t [&>td]:px-2 [&>td]:py-1 [&>td]:text-right [&>td:first-child]:text-left [&>td:nth-child(2)]:text-left" title={t.note}>
                <td className="whitespace-nowrap">{fmtDateTime(t.opened_at)}</td>
                <td>
                  <span className="mr-1 inline-block h-0.5 w-3 align-middle" style={{ background: SERIES[t.actor === "baseline" ? "baseline" : "agent"] }} />
                  {t.actor === "baseline" ? "Baseline" : "Agent"}
                </td>
                <td>{t.direction}</td>
                <td>{fmtNum(t.entry, 2)}</td>
                <td>{fmtNum(t.exit, 2)}</td>
                <td className="text-muted-foreground">{t.exit_reason?.replace(/_/g, " ")}</td>
                <td className="text-muted-foreground">{fmtNum(t.fees, 3)}</td>
                <td className={pnlClass(t.net_pnl)}>{fmtSigned(t.net_pnl)}</td>
                <td>{t.r ? `${fmtSigned(t.r)}R` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
