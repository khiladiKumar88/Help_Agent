import { OctagonX, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { api, ApiError } from "@/lib/api";
import type { AgentStatus, Market, Mode, Segment, Style } from "@/lib/types";
import { bookId } from "@/lib/utils";
import { useApp } from "@/store/app";

const STATUS: Record<AgentStatus | "offline" | "not_configured", { label: string; variant: "profit" | "warning" | "loss" | "info" | "outline" }> = {
  running: { label: "Running", variant: "profit" },
  thinking: { label: "Thinking", variant: "info" },
  rate_limited: { label: "Rate-limited", variant: "warning" },
  market_closed: { label: "Market closed", variant: "outline" },
  halted: { label: "Halted", variant: "loss" },
  stale: { label: "Data stale", variant: "warning" },
  disconnected: { label: "Feed down", variant: "loss" },
  offline: { label: "Backend offline", variant: "loss" },
  not_configured: { label: "Not configured", variant: "outline" },
};

export function StatusPill({ status }: { status: keyof typeof STATUS }) {
  const s = STATUS[status];
  return (
    <Badge variant={s.variant} aria-label={`Agent status: ${s.label}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
      {s.label}
    </Badge>
  );
}

export function TopBar() {
  const { market, segment, style, setSelection, status, conn, books } = useApp();
  const id = bookId(market, segment, style);
  const bookStatus = status?.books[id];
  const configured = Boolean(bookStatus);
  const pill: keyof typeof STATUS = conn !== "open" ? "offline" : !configured ? "not_configured" : bookStatus!.status;
  const mode: Mode = books[id]?.mode ?? bookStatus?.mode ?? "manual";
  const killEngaged = Boolean(status?.kill_switch?.engaged);
  const llm = status?.llm;
  // Co-pilot and Auto need a working analyst: without verdicts the agent would trade blind.
  const agentModes = Boolean(llm?.enabled);
  const agentHint = agentModes ? undefined : (llm?.note ?? "Needs the LLM analyst");
  const [confirmAuto, setConfirmAuto] = useState(false);
  const [modeError, setModeError] = useState<string | null>(null);

  const changeMode = (next: Mode) => {
    setModeError(null);
    if (next === "auto") {
      setConfirmAuto(true);
      return;
    }
    void api.setMode(id, next).catch((e) => setModeError(e instanceof ApiError ? e.message : "Could not change mode"));
  };

  return (
    <header className="sticky top-0 z-40 flex flex-wrap items-center gap-3 border-b bg-background/95 px-4 py-2 backdrop-blur">
      <div className="mr-2 flex items-center gap-2">
        <span className="text-base font-semibold tracking-tight">PaperMind</span>
        <Badge variant="info" title="Simulated money only — no real orders are ever placed">
          PAPER
        </Badge>
      </div>
      <Segmented<Market>
        label="Market"
        value={market}
        onChange={(v) => setSelection({ market: v })}
        options={[
          { value: "crypto", label: "Crypto" },
          { value: "india", label: "India" },
        ]}
      />
      <Segmented<Segment>
        label="Segment"
        value={segment}
        onChange={(v) => setSelection({ segment: v })}
        options={[
          { value: "spot", label: "Spot" },
          { value: "futures", label: "Futures" },
          { value: "options", label: "Options" },
        ]}
      />
      <Segmented<Style>
        label="Style"
        value={style}
        onChange={(v) => setSelection({ style: v })}
        options={[
          { value: "intraday", label: "Intraday" },
          { value: "swing", label: "Swing" },
        ]}
      />
      <Segmented<Mode>
        label="Mode"
        value={mode}
        onChange={changeMode}
        options={[
          { value: "manual", label: "Manual", disabled: !configured },
          { value: "copilot", label: "Co-pilot", disabled: !configured || !agentModes, title: agentHint },
          { value: "auto", label: "Auto", disabled: !configured || !agentModes, title: agentHint },
        ]}
      />
      <div className="ml-auto flex items-center gap-3">
        <StatusPill status={pill} />
        <LlmQuota />
        <KillSwitch engaged={killEngaged} disabled={conn !== "open"} />
      </div>
      {modeError && (
        <p role="alert" className="w-full text-xs text-loss">
          {modeError}
        </p>
      )}
      <AutoConfirm
        open={confirmAuto}
        bookId={id}
        onClose={() => setConfirmAuto(false)}
        onError={(m) => setModeError(m)}
      />
    </header>
  );
}

/** Today's LLM usage against the daily budget — the "LLM 0/—" slot in the top bar. */
export function LlmQuota() {
  const llm = useApp((s) => s.status?.llm);
  const used = llm?.calls_today ?? 0;
  const budget = llm?.daily_budget;
  const exhausted = Boolean(llm?.exhausted || llm?.paused);
  const title = !llm?.enabled
    ? (llm?.note ?? "LLM analyst is not running")
    : `${llm.model}${llm.remaining !== undefined ? ` · ${llm.remaining} calls left today` : ""}${
        llm.cached_hits ? ` · ${llm.cached_hits} answers from cache` : ""
      }`;
  return (
    <span
      className={`text-xs tabular-nums ${exhausted ? "text-warning" : "text-muted-foreground"}`}
      title={title}
      aria-label={`LLM calls today: ${used} of ${budget ?? "no budget"}`}
      data-testid="llm-quota"
    >
      LLM {used}/{budget ?? "—"}
    </span>
  );
}

/** Auto places paper trades without asking, so switching to it needs an explicit confirmation. */
export function AutoConfirm({
  open,
  bookId: id,
  onClose,
  onError,
}: {
  open: boolean;
  bookId: string;
  onClose: () => void;
  onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const confirm = async () => {
    setBusy(true);
    try {
      await api.setMode(id, "auto", true);
      onClose();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : "Could not switch to Auto");
      onClose();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Switch to Auto mode?</DialogTitle>
        <DialogDescription>
          In Auto the agent places paper trades by itself, without asking you first. It still obeys every risk rule, the
          daily loss limit and the kill switch — and it still only ever trades simulated money.
        </DialogDescription>
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={confirm} disabled={busy}>
            Yes, let the agent trade
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function KillSwitch({ engaged, disabled }: { engaged: boolean; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("manual kill switch");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fire = async () => {
    setBusy(true);
    setError(null);
    try {
      await (engaged ? api.releaseKillSwitch() : api.killSwitch(reason));
      setOpen(false);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {engaged ? (
        <Button variant="outline" size="sm" onClick={() => setOpen(true)} disabled={disabled}>
          <ShieldCheck className="h-4 w-4" /> Release kill switch
        </Button>
      ) : (
        <Button variant="destructive" onClick={() => setOpen(true)} disabled={disabled} className="font-semibold">
          <OctagonX className="h-4 w-4" /> KILL SWITCH
        </Button>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogTitle>{engaged ? "Release the kill switch?" : "Engage the kill switch?"}</DialogTitle>
          <DialogDescription>
            {engaged
              ? "Books halted by the kill switch will accept new paper trades again. Books halted by their daily loss limit stay halted."
              : "This immediately closes ALL paper positions at market in every book, cancels pending orders and halts all trading until you release it."}
          </DialogDescription>
          {!engaged && (
            <div className="mt-4 space-y-1">
              <Label htmlFor="kill-reason">Reason (logged)</Label>
              <Input id="kill-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
          )}
          {error && <p className="mt-3 text-sm text-loss">{error}</p>}
          <div className="mt-6 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant={engaged ? "default" : "destructive"} onClick={fire} disabled={busy}>
              {engaged ? "Release" : "Yes, close everything"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
