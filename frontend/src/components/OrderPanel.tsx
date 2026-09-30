import { CircleCheck, CircleX } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Segmented } from "@/components/ui/segmented";
import { api, ApiError } from "@/lib/api";
import type { BookSummary, Direction, Instrument, OrderForm, Preview } from "@/lib/types";
import { fmtMoney, fmtNum, fmtPrice, shortSymbol, slippageNote } from "@/lib/utils";
import { isPriceStale, useApp } from "@/store/app";

/** Manual paper order ticket. Every order is risk-checked by the backend (preview + on submit). */
export function OrderPanel({
  book,
  instruments,
  instrumentId,
  onInstrument,
}: {
  book: BookSummary;
  instruments: Instrument[];
  instrumentId: string;
  onInstrument: (id: string) => void;
}) {
  const [direction, setDirection] = useState<Direction>("long");
  const [orderType, setOrderType] = useState<"market" | "limit">("market");
  const [qty, setQty] = useState("");
  const [limitPrice, setLimitPrice] = useState("");
  const [sl, setSl] = useState("");
  const [target, setTarget] = useState("");
  const [leverage, setLeverage] = useState("1");
  const [preview, setPreview] = useState<{ key: string; p: Preview } | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  /** The order we just placed, so we can confirm when it actually turns into a position. */
  const [placed, setPlaced] = useState<{ tradeId: string | null; direction: Direction; qty: string; symbol: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const inst = instruments.find((i) => i.id === instrumentId);
  const tickEntry = useApp((s) => s.ticks[instrumentId]);
  const conn = useApp((s) => s.conn);
  const staleAfter = useApp((s) => s.status?.stale_after_seconds ?? 10);
  const stale = isPriceStale(tickEntry, conn, staleAfter);
  const manual = book.mode === "manual";

  const form: OrderForm | null = useMemo(() => {
    if (!qty || Number(qty) <= 0) return null;
    return {
      book_id: book.book_id,
      instrument_id: instrumentId,
      direction,
      qty,
      order_type: orderType,
      limit_price: orderType === "limit" && limitPrice ? limitPrice : undefined,
      stop_loss: sl || undefined,
      target: target || undefined,
      leverage: leverage || "1",
    };
  }, [book.book_id, instrumentId, direction, qty, orderType, limitPrice, sl, target, leverage]);

  // debounced live risk preview; refreshes as price moves (every ~2s)
  const tickBucket = tickEntry ? Math.floor(tickEntry.receivedAt / 2000) : 0;
  const formKey = form ? JSON.stringify(form) : "";
  useEffect(() => {
    if (!form) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const t = setTimeout(() => {
      api
        .preview(form)
        .then((p) => {
          setPreview({ key: JSON.stringify(form), p });
          setPreviewError(null);
        })
        .catch((e) => {
          setPreview(null);
          setPreviewError(e instanceof ApiError ? e.message : "Preview failed");
        });
    }, 250);
    return () => clearTimeout(t);
  }, [form, tickBucket]);

  useEffect(() => {
    setResult(null);
    setPlaced(null);
  }, [instrumentId]);

  /** Blank the amounts after a placed order; direction/type/leverage stay as the user set them. */
  const resetForm = () => {
    setQty("");
    setLimitPrice("");
    setSl("");
    setTarget("");
    setPreview(null);
    setPreviewError(null);
  };

  const submit = async () => {
    if (!form) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await api.place(form);
      if (r.approved) {
        setPlaced({
          tradeId: r.trade?.id ?? null,
          direction: form.direction,
          qty: form.qty,
          symbol: r.trade?.symbol ?? (inst ? shortSymbol(inst.symbol) : form.instrument_id),
        });
        setResult({ ok: true, text: "Paper order accepted — fills on the next tick." });
        resetForm(); // a placed order must not be re-submittable by accident
      } else {
        setPlaced(null);
        setResult({ ok: false, text: `Blocked by ${r.decision.failed_rule_id}: ${r.decision.message}` });
      }
    } catch (e) {
      setPlaced(null);
      setResult({ ok: false, text: e instanceof ApiError ? e.message : "Order failed" });
    } finally {
      setBusy(false);
    }
  };

  // only trust a preview computed for exactly the current form (no stale approvals)
  const current = preview && preview.key === formKey ? preview.p : null;
  const decision = current?.decision;
  const ccy = book.currency;
  const allowLeverage = book.segment === "futures";
  // a market order fills on the NEXT tick, so watch the live book for it turning into a position
  const openedPosition = placed?.tradeId
    ? book.positions.find((p) => p.id === placed.tradeId && p.status === "open" && p.avg_entry)
    : undefined;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Manual paper order</CardTitle>
        {!manual && <span className="text-xs text-warning">Switch to Manual mode to place orders</span>}
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1">
          <Label htmlFor="instrument">Instrument</Label>
          <select
            id="instrument"
            className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
            value={instrumentId}
            onChange={(e) => onInstrument(e.target.value)}
          >
            {instruments.map((i) => (
              <option key={i.id} value={i.id} className="bg-card">
                {shortSymbol(i.symbol)} ({i.exchange})
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <Segmented<Direction>
            label="Direction"
            value={direction}
            onChange={setDirection}
            options={[
              { value: "long", label: "Long / Buy" },
              { value: "short", label: "Short / Sell" },
            ]}
          />
          <Segmented<"market" | "limit">
            label="Order type"
            value={orderType}
            onChange={setOrderType}
            options={[
              { value: "market", label: "Market" },
              { value: "limit", label: "Limit" },
            ]}
          />
        </div>
        <div className="grid grid-cols-2 gap-2">
          <Field id="qty" label={`Qty${inst ? ` (step ${inst.qty_step})` : ""}`} value={qty} onChange={setQty}>
            {preview && Number(preview.p.max_qty_by_risk) > 0 && (
              <button type="button" className="text-xs text-info hover:underline" onClick={() => setQty(preview.p.max_qty_by_risk)}>
                max by risk: {preview.p.max_qty_by_risk}
              </button>
            )}
          </Field>
          {orderType === "limit" ? (
            <Field id="limit" label="Limit price" value={limitPrice} onChange={setLimitPrice} />
          ) : (
            <Field id="lev" label={allowLeverage ? "Leverage (x)" : "Leverage"} value={leverage} onChange={setLeverage} disabled={!allowLeverage} />
          )}
          <Field id="sl" label="Stop-loss (required)" value={sl} onChange={setSl} />
          <Field id="tp" label="Target (optional)" value={target} onChange={setTarget} />
          {orderType === "limit" && allowLeverage && <Field id="lev2" label="Leverage (x)" value={leverage} onChange={setLeverage} />}
        </div>

        {current && (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-md bg-muted/40 p-3 text-xs">
            <Row k="Est. entry" v={fmtNum(current.est_entry, 2)} />
            <Row k="Margin needed" v={fmtMoney(current.margin_required, ccy)} />
            <Row k="Risk if SL hit" v={fmtMoney(current.risk_amount, ccy)} />
            <Row k="Max risk allowed" v={fmtMoney(current.max_risk_allowed, ccy)} />
            <Row k="Est. fees (round trip)" v={fmtMoney(current.est_round_trip_charges, ccy, 4)} />
            <Row k="Reward : risk" v={current.reward_risk ? `${current.reward_risk} : 1` : "—"} />
            {current.slippage && (
              <>
                <Row k="Est. fill (with slippage)" v={fmtPrice(current.slippage.est_fill)} />
                <Row k="Slippage assumed" v={slippageNote(current.slippage)} />
              </>
            )}
          </dl>
        )}
        {current?.slippage && (
          <p className="text-xs text-muted-foreground" data-testid="slippage-note">
            Est. entry is {current.slippage.reference_kind === "bid" ? "the bid" : current.slippage.reference_kind === "ask" ? "the ask" : "LTP ± ½ spread"}{" "}
            ({fmtPrice(current.slippage.reference_price)}). The {current.slippage.model === "bps" ? `${fmtNum(current.slippage.bps, 0)} bps` : `${current.slippage.extra_ticks}-tick`}{" "}
            slippage model expects a fill near {fmtPrice(current.slippage.est_fill)} — about {fmtMoney(current.slippage.cost, ccy, 4)} on this size. The real
            fill comes from the next tick, so it will differ.
          </p>
        )}
        {decision && (
          <div className={`flex items-start gap-2 text-sm ${decision.approved ? "text-profit" : "text-loss"}`} data-testid="risk-verdict">
            {decision.approved ? <CircleCheck className="mt-0.5 h-4 w-4 shrink-0" /> : <CircleX className="mt-0.5 h-4 w-4 shrink-0" />}
            <span>{decision.approved ? "All risk rules pass" : `${decision.failed_rule_id}: ${decision.message}`}</span>
          </div>
        )}
        {previewError && <p className="text-sm text-loss">{previewError}</p>}

        <Button
          className="w-full"
          variant={direction === "long" ? "long" : "short"}
          disabled={!manual || !form || busy || stale || !decision?.approved}
          onClick={submit}
        >
          {stale ? "Price stale — waiting for data" : `Place paper ${direction === "long" ? "BUY" : "SELL"}`}
        </Button>
        {openedPosition ? (
          <div role="alert" className="rounded-md bg-profit/15 px-3 py-2 text-sm text-profit" data-testid="order-result">
            <span className="font-medium">Position opened</span> — {openedPosition.direction === "long" ? "LONG" : "SHORT"} {openedPosition.qty}{" "}
            {shortSymbol(openedPosition.symbol)} @ {fmtPrice(openedPosition.avg_entry)}
            <span className="block text-xs text-muted-foreground">SL {fmtPrice(openedPosition.current_sl)}{openedPosition.target ? ` · target ${fmtPrice(openedPosition.target)}` : ""} — see Open positions below.</span>
          </div>
        ) : (
          result && (
            <p role="alert" className={`text-sm ${result.ok ? "text-profit" : "text-loss"}`} data-testid="order-result">
              {result.text}
            </p>
          )
        )}
      </CardContent>
    </Card>
  );
}

function Field({
  id,
  label,
  value,
  onChange,
  disabled,
  children,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <Input id={id} inputMode="decimal" value={value} disabled={disabled} onChange={(e) => onChange(e.target.value.replace(/[^0-9.]/g, ""))} />
      {children}
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-right tabular-nums">{v}</dd>
    </>
  );
}
