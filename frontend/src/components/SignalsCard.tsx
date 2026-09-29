import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { SignalRow } from "@/lib/types";
import { fmtPrice, fmtTime } from "@/lib/utils";
import { useApp } from "@/store/app";

/** Latest scanner signals for the book. In Manual mode the agent only shows analysis — you decide. */
export function SignalsCard({ bookId, mode }: { bookId: string; mode: string }) {
  const version = useApp((s) => s.signalVersion);
  const conn = useApp((s) => s.conn);
  const [rows, setRows] = useState<SignalRow[]>([]);

  useEffect(() => {
    if (conn !== "open") return;
    api
      .signals(bookId, 15)
      .then(setRows)
      .catch(() => undefined);
  }, [bookId, version, conn]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Strategy signals</CardTitle>
        <span className="text-xs text-muted-foreground">{mode === "manual" ? "analysis only — agent does not trade in Manual" : mode}</span>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">No signals yet — strategies run on each closed candle.</p>
        ) : (
          <ul className="max-h-64 space-y-2 overflow-y-auto text-sm" data-testid="signals">
            {rows.map((s) => {
              const regime = typeof s.regime === "object" && s.regime ? String(s.regime.label ?? "") : String(s.regime ?? "");
              return (
                <li key={s.id} className="border-b pb-2 last:border-0">
                  <div className="flex items-center gap-2">
                    <Badge variant={s.direction === "long" ? "profit" : "loss"}>{s.direction.toUpperCase()}</Badge>
                    <span className="font-medium">{s.strategy_id}</span>
                    <span className="ml-auto text-xs text-muted-foreground">{fmtTime(s.ts)}</span>
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground tabular-nums">
                    entry ~{fmtPrice(s.entry_ref)} · SL {fmtPrice(s.stop_loss)} · TP {fmtPrice(s.target)} · {regime.replace("_", " ")}
                  </div>
                  <div className="mt-0.5 text-xs">{s.setup}</div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
