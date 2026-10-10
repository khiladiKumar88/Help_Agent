import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Segmented } from "@/components/ui/segmented";
import { api } from "@/lib/api";
import type { SignalDetail, SignalRow, TradeView } from "@/lib/types";
import { fmtDateTime, fmtPrice, fmtSigned, pnlClass } from "@/lib/utils";
import { selectBookId, useApp } from "@/store/app";

type Filter = "all" | "take" | "skip" | "executed";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All signals" },
  { value: "take", label: "Taken" },
  { value: "skip", label: "Skipped" },
  { value: "executed", label: "Traded" },
];

/** Query params for a filter choice. `executed` is a flag, the rest filter on `decision`. */
export function filterParams(f: Filter): Record<string, string> {
  if (f === "all") return {};
  if (f === "executed") return { executed: "true" };
  return { decision: f };
}

function decisionBadge(row: SignalRow) {
  if (row.decision === "take") return <Badge variant="profit">TAKE</Badge>;
  if (row.decision === "skip") return <Badge variant="outline">SKIP</Badge>;
  return <Badge variant="info">pending</Badge>;
}

/** Journal: every signal the scanner produced — taken and skipped — with the reasoning behind it. */
export function Journal() {
  const id = useApp(selectBookId);
  const conn = useApp((s) => s.conn);
  const status = useApp((s) => s.status);
  const tradeVersion = useApp((s) => s.tradeVersion);
  const signalVersion = useApp((s) => s.signalVersion);
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [strategy, setStrategy] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const configured = status ? id in status.books : true;

  const load = useCallback(() => {
    if (conn !== "open" || !configured) return;
    const params = { ...filterParams(filter), ...(strategy ? { strategy_id: strategy } : {}) };
    api
      .journal(id, params, 200)
      .then(setRows)
      .catch(() => undefined);
  }, [id, conn, configured, filter, strategy]);

  useEffect(load, [load, tradeVersion, signalVersion]);

  const strategies = Array.from(new Set(rows.map((r) => r.strategy_id))).sort();

  if (!configured) {
    return (
      <Card className="m-6">
        <CardContent className="py-10 text-center text-sm text-muted-foreground">
          <span className="font-mono">{id}</span> isn't set up yet.
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4 p-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-foreground">Journal</CardTitle>
          <span className="text-xs text-muted-foreground">
            Every signal, taken or skipped. Skipped ones matter most — they are how we tell whether the analyst helps.
          </span>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <Segmented<Filter> label="Show" value={filter} onChange={setFilter} options={FILTERS} />
            <div className="space-y-1">
              <label htmlFor="jrn-strategy" className="text-xs text-muted-foreground">
                Strategy
              </label>
              <select
                id="jrn-strategy"
                className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
              >
                <option value="" className="bg-card">
                  All
                </option>
                {strategies.map((s) => (
                  <option key={s} value={s} className="bg-card">
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <span className="ml-auto text-xs text-muted-foreground">{rows.length} shown</span>
          </div>

          {rows.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">No signals match that filter yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" data-testid="journal-table">
                <thead className="text-xs text-muted-foreground">
                  <tr className="border-b">
                    <th className="p-2 text-left">Time</th>
                    <th className="p-2 text-left">Strategy</th>
                    <th className="p-2 text-left">Dir</th>
                    <th className="p-2 text-right">Entry</th>
                    <th className="p-2 text-right">SL</th>
                    <th className="p-2 text-left">Decision</th>
                    <th className="p-2 text-right">Conf.</th>
                    <th className="p-2 text-left">Why</th>
                    <th className="p-2" />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id} className="border-b last:border-0">
                      <td className="p-2 whitespace-nowrap text-muted-foreground">{fmtDateTime(r.ts)}</td>
                      <td className="p-2">{r.strategy_id}</td>
                      <td className="p-2">
                        <span className={r.direction === "long" ? "text-profit" : "text-loss"}>
                          {r.direction === "long" ? "LONG" : "SHORT"}
                        </span>
                      </td>
                      <td className="p-2 text-right tabular-nums">{fmtPrice(r.entry_ref)}</td>
                      <td className="p-2 text-right tabular-nums">{fmtPrice(r.stop_loss)}</td>
                      <td className="p-2">
                        <span className="inline-flex items-center gap-1">
                          {decisionBadge(r)}
                          {r.card_status && r.card_status !== "none" && (
                            <Badge variant="outline" title="Co-pilot card">
                              {r.card_status}
                            </Badge>
                          )}
                          {r.risk_rule_blocked && (
                            <Badge variant="warning" title="Blocked by a risk rule">
                              {r.risk_rule_blocked}
                            </Badge>
                          )}
                        </span>
                      </td>
                      <td className="p-2 text-right tabular-nums">{r.confidence ?? "—"}</td>
                      <td className="max-w-[22rem] truncate p-2 text-muted-foreground" title={r.reasoning}>
                        {r.reasoning || r.decision_reason || "—"}
                      </td>
                      <td className="p-2 text-right">
                        <Button size="sm" variant="ghost" onClick={() => setOpenId(r.id)}>
                          Details
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      <SignalDrawer signalId={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}

function TradeLine({ label, trade }: { label: string; trade: TradeView | null }) {
  if (!trade) return null;
  return (
    <div className="flex items-baseline justify-between gap-3 border-b py-1 last:border-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">
        {trade.status} · entry {fmtPrice(trade.avg_entry)} → exit {fmtPrice(trade.avg_exit)} ·{" "}
        <span className={pnlClass(trade.net_pnl)}>{fmtSigned(trade.net_pnl)}</span>
        {trade.r_multiple && ` (${trade.r_multiple}R)`}
      </span>
    </div>
  );
}

/** Detail drawer: the full snapshot, the analyst's reasoning and the outcome of every shadow. */
export function SignalDrawer({ signalId, onClose }: { signalId: string | null; onClose: () => void }) {
  const [detail, setDetail] = useState<SignalDetail | null>(null);
  const [showPrompt, setShowPrompt] = useState(false);

  useEffect(() => {
    setDetail(null);
    setShowPrompt(false);
    if (!signalId) return;
    api
      .signalDetail(signalId)
      .then(setDetail)
      .catch(() => undefined);
  }, [signalId]);

  const s = detail?.signal;
  const call = detail?.llm_call;

  return (
    <Dialog open={Boolean(signalId)} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogTitle>Signal detail</DialogTitle>
        {!detail ? (
          <DialogDescription>Loading…</DialogDescription>
        ) : (
          <div className="mt-2 space-y-4 text-sm" data-testid="signal-drawer">
            <DialogDescription>
              {s?.strategy_id} · {s?.direction} · {fmtDateTime(s?.ts)}
            </DialogDescription>

            <section>
              <h3 className="mb-1 font-medium">Levels</h3>
              <div className="tabular-nums text-muted-foreground">
                entry ~{fmtPrice(s?.entry_ref)} · SL {fmtPrice(s?.stop_loss)} · target {fmtPrice(s?.target ?? null)}
              </div>
              {s?.setup && <p className="mt-1">{s.setup}</p>}
            </section>

            <section>
              <h3 className="mb-1 font-medium">Decision</h3>
              <p>
                {s?.decision} by {s?.decision_by || "—"}
                {typeof s?.confidence === "number" && ` · confidence ${s.confidence}`}
                {s?.prompt_version && ` · prompt ${s.prompt_version}`}
              </p>
              {s?.reasoning && <p className="mt-1 text-muted-foreground">{s.reasoning}</p>}
              {s?.risks_noted && s.risks_noted.length > 0 && (
                <p className="mt-1 text-warning">Risks: {s.risks_noted.join("; ")}</p>
              )}
              {s?.adjustments && Object.keys(s.adjustments).length > 0 && (
                <pre className="mt-2 overflow-x-auto rounded bg-muted/40 p-2 text-xs">
                  {JSON.stringify(s.adjustments, null, 2)}
                </pre>
              )}
            </section>

            <section>
              <h3 className="mb-1 font-medium">Outcome</h3>
              {detail.trade || detail.raw_trade || detail.baseline_trade || detail.rejected_trade ? (
                <div>
                  <TradeLine label="Agent" trade={detail.trade} />
                  <TradeLine label="Unfiltered strategy" trade={detail.raw_trade} />
                  <TradeLine label="Random baseline" trade={detail.baseline_trade} />
                  <TradeLine label="If you hadn't rejected it" trade={detail.rejected_trade} />
                </div>
              ) : (
                <p className="text-muted-foreground">No trade came from this signal.</p>
              )}
            </section>

            {s?.context && Object.keys(s.context).length > 0 && (
              <section>
                <h3 className="mb-1 font-medium">Snapshot at the signal</h3>
                <pre className="overflow-x-auto rounded bg-muted/40 p-2 text-xs">
                  {JSON.stringify({ regime: s.regime, ...s.context }, null, 2)}
                </pre>
              </section>
            )}

            {call && (
              <section>
                <h3 className="mb-1 font-medium">LLM call</h3>
                <p className="text-muted-foreground">
                  {call.provider}/{call.model} · {call.latency_ms}ms · {call.total_tokens ?? "?"} tokens
                  {call.cached && " · from cache"}
                  {!call.ok && ` · FAILED (${call.error_reason})`}
                </p>
                {call.error && <p className="mt-1 text-loss">{call.error}</p>}
                <Button size="sm" variant="ghost" className="mt-1" onClick={() => setShowPrompt((v) => !v)}>
                  {showPrompt ? "Hide" : "Show"} prompt and response
                </Button>
                {showPrompt && (
                  <div className="mt-2 space-y-2">
                    <pre className="max-h-64 overflow-auto rounded bg-muted/40 p-2 text-xs">{call.prompt || "(not stored)"}</pre>
                    <pre className="max-h-48 overflow-auto rounded bg-muted/40 p-2 text-xs">{call.response || "(none)"}</pre>
                  </div>
                )}
              </section>
            )}
          </div>
        )}
        <div className="mt-6 flex justify-end">
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
