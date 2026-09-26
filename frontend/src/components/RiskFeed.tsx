import { CircleCheck, CircleX } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { RiskDecisionRow } from "@/lib/types";
import { fmtTime } from "@/lib/utils";

/** Every risk decision, with the exact rule that blocked a rejected order. */
export function RiskFeed({ rows }: { rows: RiskDecisionRow[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Risk checks</CardTitle>
        <span className="text-xs text-muted-foreground">pure code — the LLM can never override</span>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">No orders checked yet.</p>
        ) : (
          <ul className="max-h-56 space-y-2 overflow-y-auto text-sm">
            {rows.map((r) => (
              <li key={r.id} className="flex items-start gap-2">
                {r.approved ? (
                  <CircleCheck className="mt-0.5 h-4 w-4 shrink-0 text-profit" aria-label="approved" />
                ) : (
                  <CircleX className="mt-0.5 h-4 w-4 shrink-0 text-loss" aria-label="rejected" />
                )}
                <div className="min-w-0">
                  <div className="text-xs text-muted-foreground">
                    {fmtTime(r.ts)} · {r.actor === "human" ? "You" : r.actor}
                    {r.failed_rule_id && <span className="ml-1 font-mono text-loss">{r.failed_rule_id}</span>}
                  </div>
                  <div className="truncate" title={r.message}>
                    {r.approved ? "Approved" : r.message}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
