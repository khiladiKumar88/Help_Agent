import { useEffect, useState } from "react";
import { ActorChart, EquityChart } from "@/components/Charts";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { OrderPanel } from "@/components/OrderPanel";
import { PositionsTable } from "@/components/PositionsTable";
import { PriceChart } from "@/components/PriceChart";
import { RiskFeed } from "@/components/RiskFeed";
import { SignalsCard } from "@/components/SignalsCard";
import { Card, CardContent } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { ActorPnl, EquityPoint, Instrument, LLMStatus } from "@/lib/types";
import { fmtDateTime, fmtLatency, fmtMoney, fmtSigned, pnlClass } from "@/lib/utils";
import { selectBookId, useApp } from "@/store/app";

export function Home() {
  const id = useApp(selectBookId);
  const conn = useApp((s) => s.conn);
  const status = useApp((s) => s.status);
  const book = useApp((s) => s.books[id]);
  const setBook = useApp((s) => s.setBook);
  const tradeVersion = useApp((s) => s.tradeVersion);
  const riskFeed = useApp((s) => s.riskFeed);
  const setRiskFeed = useApp((s) => s.setRiskFeed);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [instrumentId, setInstrumentId] = useState<string>("");
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [actorPnl, setActorPnl] = useState<ActorPnl | null>(null);
  const configured = status ? id in status.books : true;

  // (re)load everything for the selected book; also after reconnects
  useEffect(() => {
    if (conn !== "open" || !configured) return;
    api.book(id).then(setBook).catch(() => undefined);
    api
      .instruments(id)
      .then((list) => {
        setInstruments(list);
        setInstrumentId((cur) => (list.some((i) => i.id === cur) ? cur : (list[0]?.id ?? "")));
      })
      .catch(() => undefined);
    api
      .riskDecisions(id)
      .then((rows) => setRiskFeed(rows))
      .catch(() => undefined);
  }, [id, conn, configured, setBook, setRiskFeed]);

  // history charts: refresh on trade changes and every minute
  useEffect(() => {
    if (conn !== "open" || !configured) return;
    const load = () => {
      api.equity(id).then(setEquity).catch(() => undefined);
      api.actorPnl(id).then(setActorPnl).catch(() => undefined);
    };
    const t = setTimeout(load, 300);
    const every = setInterval(load, 60_000);
    return () => {
      clearTimeout(t);
      clearInterval(every);
    };
  }, [id, conn, configured, tradeVersion]);

  if (!configured) {
    return (
      <Card className="m-6">
        <CardContent className="py-10 text-center">
          <p className="text-lg font-medium">This book isn't set up yet</p>
          <p className="mt-2 text-sm text-muted-foreground">
            <span className="font-mono">{id}</span> — Indian markets arrive in Phase 5; crypto options in Phase 6. Available now:{" "}
            {Object.keys(status?.books ?? {}).join(", ")}.
          </p>
        </CardContent>
      </Card>
    );
  }
  if (!book) {
    return <p className="p-6 text-sm text-muted-foreground">{conn === "open" ? "Loading book…" : "Waiting for backend…"}</p>;
  }

  const inst = instruments.find((i) => i.id === instrumentId);
  const ccy = book.currency;
  const riskRows = riskFeed.filter((r) => !("book_id" in r) || (r as { book_id?: string }).book_id === id);

  return (
    <div className={`space-y-4 p-4 ${conn !== "open" ? "opacity-60" : ""}`}>
      {book.halted && (
        <div className="rounded-md bg-loss/15 px-4 py-2 text-sm text-loss">
          Book halted: {book.halt_reason}
          {book.halted_until && ` (until ${fmtDateTime(book.halted_until)})`}
        </div>
      )}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Paper balance" value={fmtMoney(book.balance, ccy)} sub={`started with ${fmtMoney(book.starting_capital, ccy, 0)}`} />
        <Stat label="Equity" value={fmtMoney(book.equity, ccy)} sub={<span className={pnlClass(book.total_pnl)}>{fmtSigned(book.total_pnl)} total</span>} />
        <Stat
          label="Today's P&L (after fees)"
          value={<span className={pnlClass(book.today_pnl)}>{fmtSigned(book.today_pnl)}</span>}
          sub={`incl. est. exit fees ${fmtMoney(book.est_exit_charges, ccy, 4)}`}
        />
        <Stat label="Margin used / free" value={fmtMoney(book.used_margin, ccy)} sub={`${fmtMoney(book.available_margin, ccy)} available`} />
      </div>

      {status?.llm && <LLMDayStat llm={status.llm} />}

      <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
        <div className="space-y-4">
          {inst && (
            <ErrorBoundary name="Price chart">
              <PriceChart instrumentId={inst.id} symbol={inst.symbol} positions={book.positions} />
            </ErrorBoundary>
          )}
          <PositionsTable positions={book.positions} currency={ccy} />
        </div>
        <div className="space-y-4">
          {instruments.length > 0 && (
            <OrderPanel book={book} instruments={instruments} instrumentId={instrumentId} onInstrument={setInstrumentId} />
          )}
          <SignalsCard bookId={id} mode={book.mode} />
          <RiskFeed rows={riskRows} />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <ErrorBoundary name="Equity chart">
          <EquityChart points={equity} currency={ccy} />
        </ErrorBoundary>
        <ErrorBoundary name="Comparison chart">
          <ActorChart pnl={actorPnl} currency={ccy} />
        </ErrorBoundary>
      </div>
    </div>
  );
}

/** Today's analyst calls at a glance: how many, how slow, and how many never answered.
 *
 * It lives on Home because the failure it exists to catch is invisible otherwise: the agent goes
 * on logging signals and placing nothing, and you only find out by opening the call log. A day
 * where every call stalls and a day where the model is simply busy look identical until you can
 * see the count, the average and the timeouts side by side.
 */
function LLMDayStat({ llm }: { llm: LLMStatus }) {
  const t = llm.today;
  if (!llm.enabled && !t?.calls) return null;
  const calls = t?.calls ?? llm.calls_today ?? 0;
  const timeouts = t?.timeouts ?? 0;
  const rateLimited = t?.rate_limited ?? 0;
  const failed = t?.errors ?? 0;
  const budget = llm.daily_budget ?? null;
  const queue = t?.avg_queue_wait_ms ?? null;

  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-2 py-3">
        <div className="text-xs font-medium text-muted-foreground">Analyst today</div>
        <Metric label="calls" value={budget ? `${calls} / ${budget}` : String(calls)} />
        <Metric
          label="avg latency"
          value={fmtLatency(t?.avg_latency_ms)}
          sub={t?.max_latency_ms ? `worst ${fmtLatency(t.max_latency_ms)}` : undefined}
        />
        <Metric
          label="timeouts"
          value={String(timeouts)}
          tone={timeouts > 0 ? "bad" : undefined}
          sub={failed > timeouts ? `${failed - timeouts} other failures` : undefined}
        />
        {rateLimited > 0 && <Metric label="rate limited" value={String(rateLimited)} tone="bad" />}
        {!!t?.cached && <Metric label="cached" value={String(t.cached)} />}
        {queue !== null && <Metric label="avg queue wait" value={fmtLatency(queue)} />}
        {llm.paused && (
          <div className="text-xs text-loss" title={llm.pause_reason ?? undefined}>
            paused — {llm.pause_reason ?? "daily budget used up"}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Metric({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "bad" }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-sm font-semibold tabular-nums ${tone === "bad" ? "text-loss" : ""}`}>{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="pt-3">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}
