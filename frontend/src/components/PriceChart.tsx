import {
  type AutoscaleInfoProvider,
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  LineStyle,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { PriceScaleNotice } from "@/components/PriceScaleNotice";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Segmented } from "@/components/ui/segmented";
import { api } from "@/lib/api";
import {
  type Bar,
  barExtent,
  bucketTime,
  candleToBar,
  sortedBars,
  type Tf,
  TF_SECONDS,
  tickBar,
} from "@/lib/chartBars";
import { cssColor } from "@/lib/cssColor";
import { fitPriceRange, type FittedRange, type PriceLevel, positionLevels } from "@/lib/priceRange";
import type { TradeView } from "@/lib/types";
import { fmtPrice, shortSymbol } from "@/lib/utils";
import { isPriceStale, useApp } from "@/store/app";

const LEVEL_COLOR: Record<PriceLevel["kind"], () => string> = {
  entry: () => cssColor("--muted-foreground"),
  sl: () => cssColor("--loss"),
  target: () => cssColor("--profit"),
};

/** The series only ever holds one (instrument, timeframe); this names it. */
const viewKey = (instrumentId: string, tf: Tf) => `${instrumentId}|${tf}`;

/** lightweight-charts brands its time type; our bars already carry plain UTC seconds. */
const asChartBar = (b: Bar) => ({ ...b, time: b.time as UTCTimestamp });

export function PriceChart({ instrumentId, symbol, positions }: { instrumentId: string; symbol: string; positions: TradeView[] }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const lastBar = useRef<Bar | null>(null);
  // The view the series currently holds data for. Ticks are applied only when it matches the
  // selected one, so a tick in flight across a timeframe switch can never reach the old series.
  const loaded = useRef<string | null>(null);
  const fitRef = useRef<FittedRange | null>(null);
  const [barRange, setBarRange] = useState<{ min: number; max: number } | null>(null);
  const [tf, setTf] = useState<Tf>("1m");
  const entry = useApp((s) => s.ticks[instrumentId]);
  const conn = useApp((s) => s.conn);
  const staleAfter = useApp((s) => s.status?.stale_after_seconds ?? 10);
  const [, force] = useState(0);

  // re-evaluate staleness every second even without new ticks
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const stale = isPriceStale(entry, conn, staleAfter);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: cssColor("--muted-foreground"), attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(255,255,255,0.05)" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
    });
    series.current = c.addSeries(CandlestickSeries, {
      upColor: cssColor("--profit"),
      downColor: cssColor("--loss"),
      wickUpColor: cssColor("--profit"),
      wickDownColor: cssColor("--loss"),
      borderVisible: false,
    });
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
      lines.current = [];
    };
  }, []);

  // load history — a new instrument or timeframe always RESETS the series with setData(), never
  // update(): the old bars are on a different grid, so appending to them is meaningless and
  // lightweight-charts rejects any bar older than the newest one it already holds.
  useEffect(() => {
    const key = viewKey(instrumentId, tf);
    let cancelled = false;
    lastBar.current = null;
    loaded.current = null; // ticks are ignored until this view's history is in
    setBarRange(null);
    api
      .candles(instrumentId, tf, 300)
      .then((r) => {
        if (cancelled || !series.current) return; // a later switch already superseded this load
        // the API's `forming` bar is always a base-timeframe (1m) candle, so it only belongs here at 1m
        const bars = sortedBars([...r.closed.map(candleToBar), tf === "1m" ? candleToBar(r.forming) : null]);
        series.current.setData(bars.map(asChartBar));
        lastBar.current = bars.at(-1) ?? null;
        loaded.current = key;
        setBarRange(barExtent(bars));
        chart.current?.timeScale().scrollToRealTime();
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [instrumentId, tf]);

  // live updates from ticks (forming bar)
  useEffect(() => {
    if (!entry || !series.current) return;
    if (loaded.current !== viewKey(instrumentId, tf)) return; // history for this view is not in yet
    const bar = tickBar(lastBar.current, bucketTime(entry.tick.ts, TF_SECONDS[tf]), Number(entry.tick.ltp));
    if (!bar) return; // unusable tick, or one belonging to a bar we have already left
    series.current.update(asChartBar(bar));
    lastBar.current = bar;
    // keep the same object when nothing moved, so this does not re-render on every tick
    setBarRange((r) =>
      r && bar.low >= r.min && bar.high <= r.max ? r : { min: Math.min(r?.min ?? bar.low, bar.low), max: Math.max(r?.max ?? bar.high, bar.high) },
    );
  }, [entry, instrumentId, tf]);

  // levels of the open positions on this instrument, and the price range that covers them
  const levels = useMemo(
    () => positions.filter((p) => p.instrument_id === instrumentId).flatMap(positionLevels),
    [positions, instrumentId],
  );
  const fit = useMemo(() => fitPriceRange(barRange?.min ?? null, barRange?.max ?? null, levels), [barRange, levels]);
  fitRef.current = fit;

  // draw the levels that fit; the rest become edge markers under the chart
  useEffect(() => {
    const s = series.current;
    if (!s) return;
    lines.current.forEach((l) => s.removePriceLine(l));
    lines.current = (fit ? fit.included : levels).map((l) =>
      s.createPriceLine({
        price: l.price,
        color: LEVEL_COLOR[l.kind](),
        lineWidth: 1,
        lineStyle: l.kind === "entry" ? LineStyle.Solid : LineStyle.Dashed,
        title: l.label,
      }),
    );
  }, [fit, levels]);

  // make the price scale include those levels (autoscale normally sees only the candles)
  useEffect(() => {
    const provider: AutoscaleInfoProvider = (base) => {
      const res = base();
      const f = fitRef.current;
      if (!f) return res;
      const r = res?.priceRange;
      return {
        ...res,
        priceRange: {
          minValue: Math.min(r?.minValue ?? f.min, f.min),
          maxValue: Math.max(r?.maxValue ?? f.max, f.max),
        },
      };
    };
    series.current?.applyOptions({ autoscaleInfoProvider: provider });
  }, [fit]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-3">
          <CardTitle className="text-foreground">{shortSymbol(symbol)}</CardTitle>
          <span className={`text-lg font-semibold tabular-nums ${stale ? "text-muted-foreground line-through decoration-1" : ""}`}>
            {fmtPrice(entry?.tick.ltp)}
          </span>
          {entry?.tick.bid && (
            <span className="text-xs text-muted-foreground tabular-nums">
              bid {fmtPrice(entry.tick.bid)} · ask {fmtPrice(entry.tick.ask)}
            </span>
          )}
          {stale && <Badge variant="warning">STALE</Badge>}
        </div>
        <Segmented<Tf>
          label="Timeframe"
          value={tf}
          onChange={setTf}
          options={(["1m", "5m", "15m", "1h"] as Tf[]).map((v) => ({ value: v, label: v }))}
        />
      </CardHeader>
      <CardContent className="space-y-2">
        <div ref={el} className="h-72 w-full" data-testid="price-chart" />
        <PriceScaleNotice fit={fit} />
      </CardContent>
    </Card>
  );
}
