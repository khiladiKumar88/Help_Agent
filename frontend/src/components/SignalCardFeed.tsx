import { CircleCheck, CircleX, Clock } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, ApiError } from "@/lib/api";
import type { SignalCard } from "@/lib/types";
import { fmtPrice, fmtTime } from "@/lib/utils";
import { useApp } from "@/store/app";

/** Reward:risk from the signal's own levels, for display only. */
export function rewardRisk(entry: string, stop: string, target: string | null): string | null {
  if (!target) return null;
  const risk = Math.abs(Number(entry) - Number(stop));
  if (!Number.isFinite(risk) || risk === 0) return null;
  const reward = Math.abs(Number(target) - Number(entry));
  if (!Number.isFinite(reward)) return null;
  return `${(reward / risk).toFixed(2)} : 1`;
}

/** Plain-English countdown for a card's remaining life. */
export function expiresIn(seconds: number | null): string {
  if (seconds === null) return "no expiry";
  if (seconds <= 0) return "expired";
  if (seconds < 60) return `${Math.ceil(seconds)}s left`;
  return `${Math.ceil(seconds / 60)}m left`;
}

/**
 * Co-pilot feed: the agent proposes, you decide. A card that expires unanswered is dropped, and
 * a rejected card is shadow-tracked so we learn what the veto cost or saved.
 */
export function SignalCardFeed({ bookId, mode }: { bookId: string; mode: string }) {
  const conn = useApp((s) => s.conn);
  const version = useApp((s) => s.signalVersion);
  const tradeVersion = useApp((s) => s.tradeVersion);
  const [cards, setCards] = useState<SignalCard[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<{ id: string; text: string } | null>(null);

  const load = useCallback(() => {
    if (conn !== "open") return;
    api
      .cards(bookId)
      .then(setCards)
      .catch(() => undefined);
  }, [bookId, conn]);

  useEffect(load, [load, version, tradeVersion]);
  // the countdown is server-authoritative, so re-fetch rather than tick down locally
  useEffect(() => {
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const resolve = async (id: string, approve: boolean) => {
    setBusy(id);
    setError(null);
    try {
      const r = await api.resolveCard(id, approve);
      setDone({
        id,
        text: approve
          ? `Approved — position opening${r.trade_id ? "" : " (blocked by a risk rule)"}`
          : "Rejected — tracked in the shadow book so we can see what it would have done",
      });
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not answer that card");
    } finally {
      setBusy(null);
    }
  };

  const copilot = mode === "copilot";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Signal cards</CardTitle>
        <span className="text-xs text-muted-foreground">
          {copilot
            ? "Approve or reject — cards expire on their own"
            : mode === "auto"
              ? "Auto mode: the agent places these itself"
              : "Co-pilot mode shows approvable cards here"}
        </span>
      </CardHeader>
      <CardContent>
        {error && <p className="mb-2 text-sm text-loss">{error}</p>}
        {done && (
          <p role="status" className="mb-2 text-sm text-profit" data-testid="card-result">
            {done.text}
          </p>
        )}
        {cards.length === 0 ? (
          <p className="py-3 text-center text-sm text-muted-foreground">
            {copilot ? "No cards waiting." : "No cards — switch to Co-pilot to approve signals yourself."}
          </p>
        ) : (
          <ul className="max-h-[32rem] space-y-3 overflow-y-auto" data-testid="signal-cards">
            {cards.map((c) => (
              <li key={c.id} className="rounded-md border p-3" data-testid={`card-${c.id}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={c.direction === "long" ? "profit" : "loss"}>{c.direction.toUpperCase()}</Badge>
                  <span className="font-medium">{c.strategy_id}</span>
                  {typeof c.confidence === "number" && (
                    <Badge variant={c.confidence >= 70 ? "info" : "warning"}>confidence {c.confidence}</Badge>
                  )}
                  <span className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <Clock className="h-3 w-3" aria-hidden />
                    {expiresIn(c.seconds_left)}
                  </span>
                </div>
                <div className="mt-1 text-xs text-muted-foreground tabular-nums">
                  entry ~{fmtPrice(c.entry_ref)} · SL {fmtPrice(c.stop_loss)} · target {fmtPrice(c.target)}
                  {rewardRisk(c.entry_ref, c.stop_loss, c.target) && ` · R:R ${rewardRisk(c.entry_ref, c.stop_loss, c.target)}`}
                  {" · "}
                  {fmtTime(c.ts)}
                </div>
                {c.setup && <p className="mt-1 text-sm">{c.setup}</p>}
                {c.reasoning && (
                  <p className="mt-1 text-sm text-muted-foreground" data-testid={`why-${c.id}`}>
                    <span className="font-medium text-foreground">Why: </span>
                    {c.reasoning}
                  </p>
                )}
                {c.risks_noted && c.risks_noted.length > 0 && (
                  <p className="mt-1 text-xs text-warning">Risks: {c.risks_noted.join("; ")}</p>
                )}
                {copilot && (
                  <div className="mt-3 flex gap-2">
                    <Button size="sm" variant="long" disabled={busy === c.id || c.expired} onClick={() => void resolve(c.id, true)}>
                      <CircleCheck className="h-4 w-4" /> Approve
                    </Button>
                    <Button size="sm" variant="outline" disabled={busy === c.id} onClick={() => void resolve(c.id, false)}>
                      <CircleX className="h-4 w-4" /> Reject
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
