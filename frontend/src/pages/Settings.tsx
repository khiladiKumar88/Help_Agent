import { CircleCheck, CircleX } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";
import type { SettingsView } from "@/lib/types";
import { useApp } from "@/store/app";

/** Turn a config key into something readable: `max_risk_per_trade_pct` -> "max risk per trade pct". */
export function label(key: string): string {
  return key.replace(/_/g, " ");
}

function Rows({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data).filter(([, v]) => v !== null && typeof v !== "object");
  if (entries.length === 0) return <p className="text-sm text-muted-foreground">Nothing configured.</p>;
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
      {entries.map(([k, v]) => (
        <div key={k} className="col-span-2 flex justify-between gap-3 border-b py-1 last:border-0">
          <dt className="text-muted-foreground">{label(k)}</dt>
          <dd className="text-right tabular-nums">{typeof v === "boolean" ? (v ? "yes" : "no") : String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Settings: read-only for now, on purpose. Risk rules are edited in the YAML under `config/`,
 * so there is exactly one source of truth and nothing in the UI can loosen a limit by accident.
 */
export function Settings() {
  const conn = useApp((s) => s.conn);
  const [s, setS] = useState<SettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (conn !== "open") return;
    api
      .settings()
      .then(setS)
      .catch(() => setError("Could not load settings"));
  }, [conn]);

  if (error) return <p className="p-6 text-sm text-loss">{error}</p>;
  if (!s) return <p className="p-6 text-sm text-muted-foreground">Loading settings…</p>;

  const llm = s.llm;
  const rt = s.llm_runtime;

  return (
    <div className="space-y-4 p-4" data-testid="settings-page">
      <p className="text-xs text-muted-foreground">
        Read-only. Edit the YAML in <span className="font-mono">config/</span> and restart — that keeps one source of
        truth for every risk limit.
      </p>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-foreground">LLM analyst</CardTitle>
            <span className="text-xs text-muted-foreground">
              {rt.enabled ? `running on ${rt.provider}` : (rt.note ?? "not running")}
            </span>
          </CardHeader>
          <CardContent className="space-y-3">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs" data-testid="llm-settings">
              <div className="col-span-2 flex justify-between border-b py-1">
                <dt className="text-muted-foreground">model</dt>
                <dd className="font-mono">{String(llm.model)}</dd>
              </div>
              <div className="col-span-2 flex justify-between border-b py-1">
                <dt className="text-muted-foreground">provider</dt>
                <dd>{String(llm.provider)}</dd>
              </div>
              <div className="col-span-2 flex justify-between border-b py-1">
                <dt className="text-muted-foreground">used today</dt>
                <dd className="tabular-nums">
                  {rt.calls_today} / {rt.daily_budget ?? "—"} calls
                  {typeof rt.cached_hits === "number" && rt.cached_hits > 0 && ` (+${rt.cached_hits} from cache)`}
                </dd>
              </div>
              {Object.entries(llm.limits).map(([k, v]) => (
                <div key={k} className="col-span-2 flex justify-between border-b py-1 last:border-0">
                  <dt className="text-muted-foreground">{label(k)}</dt>
                  <dd className="tabular-nums">{v === null ? "not tracked" : String(v)}</dd>
                </div>
              ))}
            </dl>
            <p className="text-xs text-muted-foreground">
              Google shows your account's real free-tier limits in AI Studio, so these numbers are config — set them to
              match your key.
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-foreground">API keys</CardTitle>
            <span className="text-xs text-muted-foreground">presence only — values are never shown or logged</span>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1 text-sm" data-testid="secrets">
              {Object.entries(s.secrets).map(([k, present]) => (
                <li key={k} className="flex items-center gap-2 border-b py-1 last:border-0">
                  {present ? (
                    <CircleCheck className="h-4 w-4 text-profit" aria-hidden />
                  ) : (
                    <CircleX className="h-4 w-4 text-muted-foreground" aria-hidden />
                  )}
                  <span className="font-mono text-xs">{k}</span>
                  <span className="ml-auto text-xs text-muted-foreground">{present ? "set" : "not set"}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-foreground">Market data</CardTitle>
          </CardHeader>
          <CardContent>
            <Rows data={s.data as unknown as Record<string, unknown>} />
          </CardContent>
        </Card>
      </div>

      {Object.entries(s.books).map(([id, book]) => {
        const b = book as Record<string, any>;
        return (
          <Card key={id}>
            <CardHeader>
              <CardTitle className="text-foreground">{id}</CardTitle>
              <span className="text-xs text-muted-foreground">
                mode {String(b.mode)} · {String(b.currency)} · starts with {String(b.starting_capital)}
              </span>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              <div>
                <h3 className="mb-1 text-xs font-medium">Risk rules</h3>
                <Rows data={b.risk ?? {}} />
              </div>
              <div>
                <h3 className="mb-1 text-xs font-medium">Slippage</h3>
                <Rows data={b.slippage ?? {}} />
                <h3 className="mt-3 mb-1 text-xs font-medium">Co-pilot</h3>
                <Rows data={b.copilot ?? {}} />
              </div>
              <div>
                <h3 className="mb-1 text-xs font-medium">Strategies</h3>
                {(b.strategies ?? []).length === 0 ? (
                  <p className="text-xs text-muted-foreground">None enabled.</p>
                ) : (
                  <ul className="space-y-1 text-xs" data-testid={`strategies-${id}`}>
                    {(b.strategies as { id: string; enabled: boolean; params: Record<string, number> }[]).map((st) => (
                      <li key={st.id} className="flex items-center gap-2 border-b py-1 last:border-0">
                        <Badge variant={st.enabled ? "profit" : "outline"}>{st.enabled ? "on" : "off"}</Badge>
                        <span className="font-mono">{st.id}</span>
                        {Object.keys(st.params ?? {}).length > 0 && (
                          <span className="ml-auto text-muted-foreground">
                            {Object.entries(st.params)
                              .map(([k, v]) => `${k}=${v}`)
                              .join(" ")}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                <h3 className="mt-3 mb-1 text-xs font-medium">Charges</h3>
                <Rows data={b.charges ?? {}} />
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
