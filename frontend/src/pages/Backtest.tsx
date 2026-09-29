import { Database, Download, Play, Square } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { ResultView } from "@/components/backtest/ResultView";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { api, ApiError } from "@/lib/api";
import type { BacktestResult, Coverage, JobBody, JobRow, StrategyInfo } from "@/lib/types";
import { isFinished, useJob } from "@/lib/useJob";
import { fmtDateTime, fmtNum } from "@/lib/utils";
import { selectBookId, useApp } from "@/store/app";

type Tab = "backtest" | "walkforward" | "replay";
const DAY = 86_400_000;
const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const toIso = (day: string) => `${day}T00:00:00Z`;

export function BacktestPage() {
  const bookId = useApp(selectBookId);
  const status = useApp((s) => s.status);
  const conn = useApp((s) => s.conn);
  const configured = status ? bookId in status.books : true;
  const liveExchange = status?.provider.exchange && status.provider.exchange !== "replay" ? status.provider.exchange : "binanceusdm";

  const [symbols, setSymbols] = useState<string[]>([]);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [exchange, setExchange] = useState<string>(liveExchange === "simulated" ? "simulated" : liveExchange);
  const [symbol, setSymbol] = useState("");
  const [tf, setTf] = useState("15m"); // 15m: fast; 5m: more realistic intrabar stop/target fills
  const [signalTf, setSignalTf] = useState("15m");
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [dlStart, setDlStart] = useState(isoDay(Date.now() - 200 * DAY));
  const [tab, setTab] = useState<Tab>("backtest");
  const [runId, setRunId] = useState<string | null>(null);
  const [dlRunId, setDlRunId] = useState<string | null>(null);
  const [recent, setRecent] = useState<JobRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (conn !== "open" || !configured) return;
    api
      .strategies()
      .then(setStrategies)
      .catch(() => undefined);
    api
      .bookConfig(bookId)
      .then((c) => {
        setSymbols(c.book.instruments);
        setSymbol((cur) => (c.book.instruments.includes(cur) ? cur : (c.book.instruments[0] ?? "")));
        setSignalTf(c.book.timeframes.signal);
      })
      .catch(() => undefined);
  }, [bookId, conn, configured]);

  const loadCoverage = useCallback(() => {
    if (!symbol) return;
    api
      .coverage(exchange, symbol, tf)
      .then(setCoverage)
      .catch(() => setCoverage(null));
  }, [exchange, symbol, tf]);
  useEffect(loadCoverage, [loadCoverage]);

  const loadRecent = useCallback(() => {
    api
      .jobs(15)
      .then(setRecent)
      .catch(() => undefined);
  }, []);
  useEffect(loadRecent, [loadRecent]);

  const { job } = useJob(runId);
  const { job: dlJob } = useJob(dlRunId);
  useEffect(() => {
    if (isFinished(dlJob)) {
      loadCoverage();
      loadRecent();
    }
  }, [dlJob, loadCoverage, loadRecent]);
  useEffect(() => {
    if (isFinished(job)) loadRecent();
  }, [job, loadRecent]);

  const download = async () => {
    setError(null);
    try {
      const r = await api.download({ exchange, symbol, timeframe: tf, start: toIso(dlStart) });
      setDlRunId(r.run_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Download failed to start");
    }
  };

  const start = async (body: JobBody) => {
    setError(null);
    try {
      const r = await api.startJob(body);
      setRunId(r.run_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to start");
    }
  };

  if (!configured) {
    return <p className="p-6 text-sm text-muted-foreground">Book {bookId} is not configured — pick Crypto › Futures › Intraday.</p>;
  }

  const common = { book_id: bookId, exchange, symbol, base_tf: tf };
  const result = job?.status === "done" && job.result && "agent" in job.result ? (job.result as BacktestResult) : null;
  const tfOk = TF_SECONDS[signalTf]! % TF_SECONDS[tf]! === 0 && TF_SECONDS[tf]! <= TF_SECONDS[signalTf]!;

  return (
    <div className="space-y-4 p-4">
      <p className="text-sm text-muted-foreground">
        Backtests and replays run the <b>same engine, risk rules and paper broker</b> as live trading, on a simulated clock, using{" "}
        <b>only locally stored history</b>. Fees and slippage are always on. Every result is compared with a random-entry baseline and
        buy-and-hold.
      </p>
      {error && <div className="rounded-md bg-loss/15 px-4 py-2 text-sm text-loss">{error}</div>}

      <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-foreground">
              <Database className="h-4 w-4" /> Market data (local cache)
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
              <Select id="exchange" label="Source" value={exchange} onChange={setExchange} options={[...new Set([liveExchange, "simulated"])]} />
              <Select id="symbol" label="Symbol" value={symbol} onChange={setSymbol} options={symbols} />
              <Select id="tf" label="Bar size" value={tf} onChange={setTf} options={["1m", "5m", "15m", "1h"]} />
              <div className="space-y-1">
                <Label htmlFor="dl-start">Download from</Label>
                <Input id="dl-start" type="date" value={dlStart} onChange={(e) => setDlStart(e.target.value)} />
              </div>
            </div>
            {exchange === "simulated" && (
              <p className="text-xs text-info">"simulated" is a deterministic synthetic price series for offline testing — not real prices.</p>
            )}
            <CoverageView c={coverage} />
            <div className="flex items-center gap-3">
              <Button variant="outline" onClick={download} disabled={!symbol || (dlJob !== null && !isFinished(dlJob))}>
                <Download className="h-4 w-4" /> Download / update
              </Button>
              {dlJob && <JobProgress job={dlJob} />}
            </div>
            {!tfOk && <p className="text-xs text-warning">Bar size must evenly divide the book's signal timeframe ({signalTf}).</p>}
          </CardContent>
        </Card>
        <RecentRuns rows={recent} onOpen={setRunId} active={runId} />
      </div>

      <Card>
        <CardHeader>
          <Segmented<Tab>
            label="Run type"
            value={tab}
            onChange={setTab}
            options={[
              { value: "backtest", label: "Backtest" },
              { value: "walkforward", label: "Walk-forward" },
              { value: "replay", label: "Replay a day" },
            ]}
          />
          <span className="text-xs text-muted-foreground">signal timeframe {signalTf}</span>
        </CardHeader>
        <CardContent>
          {tab === "backtest" && <BacktestForm strategies={strategies} disabled={!symbol || !tfOk} onRun={(b) => start({ ...common, ...b })} />}
          {tab === "walkforward" && <WalkForwardForm strategies={strategies} disabled={!symbol || !tfOk} onRun={(b) => start({ ...common, ...b })} />}
          {tab === "replay" && <ReplayForm disabled={!symbol || !tfOk} onRun={(b) => start({ ...common, ...b })} />}
        </CardContent>
      </Card>

      {job && job.status !== "done" && (
        <Card>
          <CardContent className="flex items-center gap-4 pt-4">
            <JobProgress job={job} />
            {!isFinished(job) && (
              <Button size="sm" variant="ghost" onClick={() => void api.cancelJob(job.id)}>
                <Square className="h-3.5 w-3.5" /> Cancel
              </Button>
            )}
          </CardContent>
        </Card>
      )}
      {result && (
        <ErrorBoundary name="Result">
          <ResultView r={result} />
        </ErrorBoundary>
      )}
    </div>
  );
}

const TF_SECONDS: Record<string, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };

function Select({ id, label, value, onChange, options }: { id: string; label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <select id={id} className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o} className="bg-card">
            {o}
          </option>
        ))}
      </select>
    </div>
  );
}

function CoverageView({ c }: { c: Coverage | null }) {
  if (!c || c.bars === 0) {
    return <p className="text-sm text-muted-foreground" data-testid="coverage">No data stored yet for this symbol and bar size — download it first.</p>;
  }
  return (
    <div className="space-y-1 text-sm" data-testid="coverage">
      <div className="flex flex-wrap items-center gap-2">
        <span>
          {fmtDateTime(c.first)} → {fmtDateTime(c.last)}
        </span>
        <Badge variant="outline">{c.bars.toLocaleString()} bars</Badge>
        <Badge variant={c.missing_bars === 0 ? "profit" : "warning"}>
          {c.missing_bars === 0 ? "no gaps" : `${c.missing_bars.toLocaleString()} missing bars (${fmtNum(c.complete_pct, 1)}% complete)`}
        </Badge>
      </div>
      {c.gaps.length > 0 && (
        <ul className="text-xs text-muted-foreground">
          {c.gaps.slice(0, 5).map((g) => (
            <li key={g.start}>
              gap {fmtDateTime(g.start)} → {fmtDateTime(g.end)} ({g.missing_bars} bars)
            </li>
          ))}
          {c.gaps.length > 5 && <li>…and {c.gaps.length - 5} more</li>}
        </ul>
      )}
    </div>
  );
}

function JobProgress({ job }: { job: JobRow }) {
  const pct = Math.round((job.progress ?? 0) * 100);
  if (job.status === "error") return <span className="text-sm text-loss">Failed: {job.error}</span>;
  if (job.status === "cancelled") return <span className="text-sm text-muted-foreground">Cancelled</span>;
  if (job.status === "done") return <span className="text-sm text-profit">{job.result && "summary" in job.result ? job.result.summary : "Done"}</span>;
  return (
    <div className="flex min-w-64 flex-1 items-center gap-3" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
        <div className="h-full bg-info transition-all" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-40 truncate text-xs text-muted-foreground">
        {job.status === "queued" ? "queued…" : `${pct}% ${job.message ?? ""}`}
      </span>
    </div>
  );
}

function RecentRuns({ rows, onOpen, active }: { rows: JobRow[]; onOpen: (id: string) => void; active: string | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Recent runs</CardTitle>
      </CardHeader>
      <CardContent className="max-h-72 space-y-1 overflow-y-auto">
        {rows.length === 0 && <p className="text-sm text-muted-foreground">Nothing yet.</p>}
        {rows.map((r) => (
          <button
            key={r.id}
            onClick={() => r.kind !== "download" && onOpen(r.id)}
            className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-accent ${active === r.id ? "bg-accent" : ""}`}
          >
            <Badge variant={r.status === "done" ? "profit" : r.status === "error" ? "loss" : "outline"}>{r.kind}</Badge>
            <span className="min-w-0 flex-1 truncate text-muted-foreground" title={r.summary ?? r.error ?? ""}>
              {String(r.spec.strategy_id ?? r.spec.symbol ?? "")} · {fmtDateTime(r.created_at)}
            </span>
            {r.verdict?.beat_baseline != null && (
              <Badge variant={r.verdict.beat_baseline ? "profit" : "loss"}>{r.verdict.beat_baseline ? "beat baseline" : "lost to baseline"}</Badge>
            )}
          </button>
        ))}
      </CardContent>
    </Card>
  );
}

function StrategyPicker({ strategies, value, onChange }: { strategies: StrategyInfo[]; value: string; onChange: (v: string) => void }) {
  const s = strategies.find((x) => x.id === value);
  return (
    <div className="space-y-1">
      <Label htmlFor="strategy">Strategy</Label>
      <select id="strategy" className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm" value={value} onChange={(e) => onChange(e.target.value)}>
        {strategies.map((x) => (
          <option key={x.id} value={x.id} className="bg-card">
            {x.name}
          </option>
        ))}
      </select>
      {s && (
        <p className="text-xs text-muted-foreground">
          {s.description} <span className="font-mono">defaults: {Object.entries(s.default_params).map(([k, v]) => `${k}=${v}`).join(", ")}</span>
        </p>
      )}
    </div>
  );
}

function useStrategy(strategies: StrategyInfo[]): [string, (v: string) => void] {
  const [sid, setSid] = useState("");
  const first = strategies[0]?.id ?? "";
  useEffect(() => {
    if (!sid && first) setSid(first);
  }, [sid, first]);
  return [sid, setSid];
}

function BacktestForm({ strategies, disabled, onRun }: { strategies: StrategyInfo[]; disabled: boolean; onRun: (b: Partial<JobBody> & { kind: "backtest" }) => void }) {
  const [sid, setSid] = useStrategy(strategies);
  const [from, setFrom] = useState(isoDay(Date.now() - 31 * DAY));
  const [to, setTo] = useState(isoDay(Date.now() - DAY));
  return (
    <div className="grid gap-3 md:grid-cols-[2fr_1fr_1fr_auto] md:items-end">
      <StrategyPicker strategies={strategies} value={sid} onChange={setSid} />
      <DateField id="bt-from" label="From" value={from} onChange={setFrom} />
      <DateField id="bt-to" label="To" value={to} onChange={setTo} />
      <Button disabled={disabled || !sid} onClick={() => onRun({ kind: "backtest", strategy_id: sid, start: toIso(from), end: toIso(to) })}>
        <Play className="h-4 w-4" /> Run backtest
      </Button>
      <p className="text-xs text-muted-foreground md:col-span-4">Single run with the strategy's default parameters (no optimization, so no fitting to this period).</p>
    </div>
  );
}

function WalkForwardForm({ strategies, disabled, onRun }: { strategies: StrategyInfo[]; disabled: boolean; onRun: (b: Partial<JobBody> & { kind: "walkforward" }) => void }) {
  const [sid, setSid] = useStrategy(strategies);
  const [from, setFrom] = useState(isoDay(Date.now() - 181 * DAY));
  const [to, setTo] = useState(isoDay(Date.now() - DAY));
  const [train, setTrain] = useState("60");
  const [test, setTest] = useState("30");
  const s = strategies.find((x) => x.id === sid);
  const days = (Date.parse(to) - Date.parse(from)) / DAY;
  const folds = Math.max(0, Math.floor((days - Number(train) - Number(test)) / Number(test)) + 1);
  return (
    <div className="grid gap-3 md:grid-cols-[2fr_1fr_1fr_0.6fr_0.6fr_auto] md:items-end">
      <StrategyPicker strategies={strategies} value={sid} onChange={setSid} />
      <DateField id="wf-from" label="From" value={from} onChange={setFrom} />
      <DateField id="wf-to" label="To" value={to} onChange={setTo} />
      <div className="space-y-1">
        <Label htmlFor="wf-train">Train days</Label>
        <Input id="wf-train" value={train} onChange={(e) => setTrain(e.target.value.replace(/\D/g, ""))} />
      </div>
      <div className="space-y-1">
        <Label htmlFor="wf-test">Test days</Label>
        <Input id="wf-test" value={test} onChange={(e) => setTest(e.target.value.replace(/\D/g, ""))} />
      </div>
      <Button
        disabled={disabled || !sid || folds < 1}
        onClick={() => onRun({ kind: "walkforward", strategy_id: sid, start: toIso(from), end: toIso(to), train_days: Number(train), test_days: Number(test) })}
      >
        <Play className="h-4 w-4" /> Run walk-forward
      </Button>
      <p className="text-xs text-muted-foreground md:col-span-6">
        {folds} fold{folds === 1 ? "" : "s"} × {s?.grid_size ?? "?"} parameter combos (small grid). Parameters are chosen on each training window;{" "}
        <b>only the following unseen test window is reported</b>.
      </p>
    </div>
  );
}

function ReplayForm({ disabled, onRun }: { disabled: boolean; onRun: (b: Partial<JobBody> & { kind: "replay" }) => void }) {
  const [day, setDay] = useState(isoDay(Date.now() - 2 * DAY));
  const [speed, setSpeed] = useState("0");
  return (
    <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
      <DateField id="rp-day" label="Day to replay" value={day} onChange={setDay} />
      <div className="space-y-1">
        <Label htmlFor="rp-speed">Speed</Label>
        <select id="rp-speed" className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm" value={speed} onChange={(e) => setSpeed(e.target.value)}>
          <option value="0" className="bg-card">As fast as possible</option>
          <option value="3600" className="bg-card">3600× (1 day in 24 s)</option>
          <option value="600" className="bg-card">600× (1 day in 2.4 min)</option>
        </select>
      </div>
      <Button disabled={disabled} onClick={() => onRun({ kind: "replay", day: `${day}T12:00:00Z`, speed: Number(speed) })}>
        <Play className="h-4 w-4" /> Replay day
      </Button>
      <p className="text-xs text-muted-foreground md:col-span-3">
        Runs every strategy enabled for this book through the full pipeline (scanner → agent → risk → paper broker) and the random baseline.
      </p>
    </div>
  );
}

function DateField({ id, label, value, onChange }: { id: string; label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} type="date" value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
