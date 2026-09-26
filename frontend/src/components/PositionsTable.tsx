import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import type { TradeView } from "@/lib/types";
import { fmtNum, fmtPrice, fmtSigned, pnlClass, shortSymbol } from "@/lib/utils";

export function PositionsTable({ positions, currency }: { positions: TradeView[]; currency: string }) {
  const [editing, setEditing] = useState<TradeView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Action failed");
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Open positions</CardTitle>
        <span className="text-xs text-muted-foreground">P&L after fees (incl. estimated exit fees)</span>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        {error && <p className="mb-2 text-sm text-loss">{error}</p>}
        {positions.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">No open paper positions.</p>
        ) : (
          <table className="w-full text-sm tabular-nums">
            <thead className="text-xs text-muted-foreground">
              <tr className="[&>th]:px-2 [&>th]:py-1.5 [&>th]:text-right [&>th:first-child]:text-left">
                <th>Instrument</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>Stop</th>
                <th>Target</th>
                <th>Fees paid</th>
                <th>P&L ({currency})</th>
                <th>R</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id} className="border-t [&>td]:px-2 [&>td]:py-2 [&>td]:text-right [&>td:first-child]:text-left">
                  <td>
                    <div className="font-medium">{shortSymbol(p.symbol)}</div>
                    <div className="text-xs text-muted-foreground">{p.actor === "human" ? "You" : p.actor}</div>
                  </td>
                  <td>
                    <Badge variant={p.direction === "long" ? "profit" : "loss"}>{p.direction === "long" ? "LONG" : "SHORT"}</Badge>
                  </td>
                  <td>{p.qty}</td>
                  <td>{p.status === "pending" ? <Badge variant="outline">pending</Badge> : fmtPrice(p.avg_entry)}</td>
                  <td>
                    {fmtPrice(p.mark_price)}
                    {p.stale && <Badge variant="warning" className="ml-1">stale</Badge>}
                  </td>
                  <td>{fmtPrice(p.current_sl)}</td>
                  <td>{fmtPrice(p.target)}</td>
                  <td className="text-muted-foreground">{fmtNum(p.charges, 4)}</td>
                  <td className={`font-medium ${pnlClass(p.unrealized_net)}`}>{fmtSigned(p.unrealized_net, 2)}</td>
                  <td className={pnlClass(p.unrealized_r)}>{p.unrealized_r ? `${fmtSigned(p.unrealized_r)}R` : "—"}</td>
                  <td className="whitespace-nowrap">
                    {p.status === "open" ? (
                      <>
                        <Button size="sm" variant="ghost" onClick={() => setEditing(p)}>
                          Edit
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => act(() => api.closeTrade(p.id))}>
                          Close
                        </Button>
                      </>
                    ) : (
                      <Button size="sm" variant="outline" onClick={() => act(() => api.cancelTrade(p.id))}>
                        Cancel
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
      {editing && <EditStops trade={editing} onClose={() => setEditing(null)} />}
    </Card>
  );
}

function EditStops({ trade, onClose }: { trade: TradeView; onClose: () => void }) {
  const [sl, setSl] = useState(trade.current_sl);
  const [target, setTarget] = useState(trade.target ?? "");
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setError(null);
    try {
      await api.modifyTrade(trade.id, {
        stop_loss: sl !== trade.current_sl ? sl : undefined,
        target: target && target !== trade.target ? target : undefined,
        clear_target: !target && trade.target !== null,
      });
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.ruleId ? `${e.ruleId}: ` : ""}${e.message}` : "Failed");
    }
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Edit stop / target — {shortSymbol(trade.symbol)}</DialogTitle>
        <DialogDescription>Loosening the stop is re-checked against the max risk per trade.</DialogDescription>
        <div className="mt-4 grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <Label htmlFor="edit-sl">Stop-loss</Label>
            <Input id="edit-sl" value={sl} onChange={(e) => setSl(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="edit-tp">Target (blank = none)</Label>
            <Input id="edit-tp" value={target} onChange={(e) => setTarget(e.target.value)} />
          </div>
        </div>
        {error && <p className="mt-3 text-sm text-loss">{error}</p>}
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save}>Save</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
