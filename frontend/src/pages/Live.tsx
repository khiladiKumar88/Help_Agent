import { useEffect, useState } from "react";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { PositionsTable } from "@/components/PositionsTable";
import { PriceChart } from "@/components/PriceChart";
import { SignalCardFeed } from "@/components/SignalCardFeed";
import { SignalsCard } from "@/components/SignalsCard";
import { Card, CardContent } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { Instrument, LLMStatus } from "@/lib/types";
import { selectBookId, useApp } from "@/store/app";

/** Live: chart with the open position's levels, plus the agent's signal-card feed beside it. */
export function Live() {
  const id = useApp(selectBookId);
  const conn = useApp((s) => s.conn);
  const status = useApp((s) => s.status);
  const book = useApp((s) => s.books[id]);
  const setBook = useApp((s) => s.setBook);
  const signalVersion = useApp((s) => s.signalVersion);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [instrumentId, setInstrumentId] = useState("");
  const [llm, setLlm] = useState<LLMStatus | null>(null);
  const configured = status ? id in status.books : true;

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
  }, [id, conn, configured, setBook, signalVersion]);

  useEffect(() => {
    if (conn !== "open") return;
    api.llmStatus().then(setLlm).catch(() => undefined);
  }, [conn, signalVersion]);

  if (!configured) {
    return (
      <Card className="m-6">
        <CardContent className="py-10 text-center text-sm text-muted-foreground">
          <span className="font-mono">{id}</span> isn't set up yet.
        </CardContent>
      </Card>
    );
  }
  if (!book) {
    return <p className="p-6 text-sm text-muted-foreground">{conn === "open" ? "Loading book…" : "Waiting for backend…"}</p>;
  }

  const inst = instruments.find((i) => i.id === instrumentId);
  const mode = book.mode;

  return (
    <div className={`space-y-4 p-4 ${conn !== "open" ? "opacity-60" : ""}`}>
      {llm && !llm.enabled && (
        <div className="rounded-md bg-warning/15 px-4 py-2 text-sm text-warning" data-testid="llm-off-banner">
          The LLM analyst is not running, so Co-pilot and Auto are unavailable. {llm.note}
        </div>
      )}
      {llm?.paused && (
        <div className="rounded-md bg-warning/15 px-4 py-2 text-sm text-warning" data-testid="llm-paused-banner">
          Agent paused: {llm.pause_reason ?? "the daily LLM budget is used up"}. It will not trade without a verdict.
        </div>
      )}
      {book.halted && (
        <div className="rounded-md bg-loss/15 px-4 py-2 text-sm text-loss">Book halted: {book.halt_reason}</div>
      )}
      <div className="grid gap-4 xl:grid-cols-[1fr_420px]">
        <div className="space-y-4">
          {inst && (
            <ErrorBoundary name="Price chart">
              <PriceChart instrumentId={inst.id} symbol={inst.symbol} positions={book.positions} />
            </ErrorBoundary>
          )}
          <PositionsTable positions={book.positions} currency={book.currency} />
        </div>
        <div className="space-y-4">
          <ErrorBoundary name="Signal cards">
            <SignalCardFeed bookId={id} mode={mode} />
          </ErrorBoundary>
          <SignalsCard bookId={id} mode={mode} llm={llm} />
        </div>
      </div>
    </div>
  );
}
