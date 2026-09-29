import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ACTOR_COLORS } from "@/lib/seriesColors";
import type { ActorPnl, EquityPoint } from "@/lib/types";
import { fmtAxisTime, fmtDateTime, fmtNum, fmtSigned, timeTicks } from "@/lib/utils";

// Categorical slots 1-3 of the reference palette, dark-mode steps (validated on the dark card surface).
const SERIES = ACTOR_COLORS;
const LABEL = { human: "You", agent: "Agent", baseline: "Baseline" } as const;
const AXIS = { stroke: "var(--muted-foreground)", fontSize: 11, tickLine: false, axisLine: false } as const;
const GRID = "rgba(255,255,255,0.06)";

function TooltipBox({ active, payload, label }: { active?: boolean; payload?: { name: string; value: number; color: string }[]; label?: number }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 text-muted-foreground">{fmtDateTime(new Date(label ?? 0).toISOString())}</div>
      {payload.map((p) => (
        <div key={p.name} className="flex items-center gap-2">
          <span className="inline-block h-0.5 w-3" style={{ background: p.color }} aria-hidden />
          <span className="font-semibold tabular-nums text-foreground">{fmtNum(p.value, 2)}</span>
          <span className="text-muted-foreground">{p.name}</span>
        </div>
      ))}
    </div>
  );
}

export function EquityChart({ points, currency }: { points: EquityPoint[]; currency: string }) {
  const data = points.map((p) => ({ t: new Date(p.ts).getTime(), equity: Number(p.equity) }));
  const digits = tickDigits(data.map((d) => d.equity));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Equity curve ({currency})</CardTitle>
        <span className="text-xs text-muted-foreground">balance + open P&L, sampled every minute</span>
      </CardHeader>
      <CardContent className="h-56">
        {data.length < 2 ? (
          <Empty text="The curve appears after the first few minutes of running." />
        ) : (
          <ResponsiveContainer>
            <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke={GRID} />
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} ticks={timeTicks(data.map((d) => d.t), 5)} tickFormatter={(t: number) => fmtAxisTime(t, data[data.length - 1]!.t - data[0]!.t)} {...AXIS} />
              <YAxis domain={["auto", "auto"]} width={72} tickFormatter={(v: number) => fmtNum(v, digits)} {...AXIS} />
              <Tooltip content={<TooltipBox />} cursor={{ stroke: "var(--muted-foreground)", strokeWidth: 1 }} />
              <Line type="linear" dataKey="equity" name="Equity" stroke={SERIES.human} strokeWidth={2} dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}

export function ActorChart({ pnl, currency }: { pnl: ActorPnl | null; currency: string }) {
  const actors = ["human", "agent", "baseline"] as const;
  // one merged time axis; each series carries its cumulative value forward
  const events = actors
    .flatMap((a) => (pnl?.series[a] ?? []).map((p) => ({ a, t: new Date(p.ts).getTime(), v: Number(p.cum_net_pnl) })))
    .sort((x, y) => x.t - y.t);
  const cur: Record<string, number | undefined> = {};
  const data: ({ t: number } & Record<string, number | undefined>)[] = [];
  for (const e of events) {
    cur[e.a] = e.v;
    const last = data[data.length - 1];
    if (last && last.t === e.t) Object.assign(last, cur);
    else data.push({ ...cur, t: e.t });
  }
  const present = actors.filter((a) => (pnl?.series[a]?.length ?? 0) > 0);
  const digits = tickDigits([0, ...events.map((e) => e.v)]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">You vs Agent vs Baseline</CardTitle>
        <span className="text-xs text-muted-foreground">cumulative net P&L of closed trades ({currency})</span>
      </CardHeader>
      <CardContent className="h-56">
        {data.length === 0 ? (
          <Empty text="No closed trades yet. The agent and random baseline start trading in Phases 2–3." />
        ) : (
          <ResponsiveContainer>
            <LineChart data={data} margin={{ top: 8, right: 56, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} stroke={GRID} />
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} ticks={timeTicks(data.map((d) => d.t), 5)} tickFormatter={(t: number) => fmtAxisTime(t, data[data.length - 1]!.t - data[0]!.t)} {...AXIS} />
              <YAxis width={56} domain={["auto", "auto"]} tickFormatter={(v: number) => fmtNum(v, digits)} {...AXIS} />
              <Tooltip content={<TooltipBox />} cursor={{ stroke: "var(--muted-foreground)", strokeWidth: 1 }} />
              {present.length >= 2 && <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />}
              {present.map((a) => (
                <Line
                  key={a}
                  type="stepAfter"
                  dataKey={a}
                  name={LABEL[a]}
                  stroke={SERIES[a]}
                  strokeWidth={2}
                  strokeDasharray={a === "baseline" ? "5 4" : undefined}
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                  label={(props: { index?: number; x?: number | string; y?: number | string; value?: unknown }) =>
                    props.index === data.length - 1 && typeof props.value === "number" ? (
                      <text key={a} x={Number(props.x ?? 0) + 6} y={Number(props.y ?? 0)} dy={4} fontSize={11} fill="var(--foreground)">
                        {LABEL[a]} {fmtSigned(props.value)}
                      </text>
                    ) : (
                      <g key={`${a}-${props.index}`} />
                    )
                  }
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}

/** Pick tick decimals from the data span so small moves don't render as "1,000 1,000 1,000". */
function tickDigits(values: number[]): number {
  if (!values.length) return 0;
  const span = Math.max(...values) - Math.min(...values);
  return span < 2 ? 2 : span < 20 ? 1 : 0;
}

function Empty({ text }: { text: string }) {
  return <div className="flex h-full items-center justify-center text-center text-sm text-muted-foreground">{text}</div>;
}
