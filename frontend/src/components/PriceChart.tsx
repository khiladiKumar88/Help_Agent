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
import { cssColor } from "@/lib/cssColor";
import { fitPriceRange, type FittedRange, type PriceLevel, positionLevels } from "@/lib/priceRange";
import type { Candle, TradeView } from "@/lib/types";
import { fmtPrice, shortSymbol } from "@/lib/utils";
import { isPriceStale, useApp } from "@/store/app";

type Tf = "1m" | "5m" | "15m" | "1h";
const TF_SECONDS: Record<Tf, number> = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };


const LEVEL_COLOR: Record<PriceLevel["kind"], () => string> = {
  entry: () => cssColor("--muted-foreground"),
  sl: () => cssColor("--loss"),
  target: () => cssColor("--profit"),
};

const barExtent = (bars: { high: number; low: number }[]) =>
  bars.length ? { min: Math.min(...bars.map((b) => b.low)), max: Math.max(...bars.map((b) => b.high)) } : null;

const toBar = (c: Candle) => ({
  time: (new Date(c.ts_open).getTime() / 1000) as UTCTimestamp,
  open: Number(c.open),
  high: Number(c.high),
  low: Number(c.low),
  close: Number(c.close),
});

export function PriceChart({ instrumentId, symbol, positions }: { instrumentId: string; symbol: string; positions: TradeView[] }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const lastBar = useRef<ReturnType<typeof toBar> | null>(null);
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

  // load history
  useEffect(() => {
    let cancelled = false;
    lastBar.current = null;
    setBarRange(null);
    api
      .candles(instrumentId, tf, 300)
      .then((r) => {
        if (cancelled || !series.current) return;
        const bars = r.closed.map(toBar);
        if (tf === "1m" && r.forming) bars.push(toBar(r.forming));
        series.current.setData(bars);
        lastBar.current = bars.at(-1) ?? null;
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
    const px = Number(entry.tick.ltp);
    const secs = TF_SECONDS[tf];
    const t = (Math.floor(new Date(entry.tick.ts).getTime() / 1000 / secs) * secs) as UTCTimestamp;
    const prev = lastBar.current;
    if (prev && t < prev.time) return;
    const bar =
      prev && prev.time === t
        ? { ...prev, high: Math.max(prev.high, px), low: Math.min(prev.low, px), close: px }
        : { time: t, open: prev?.close ?? px, high: px, low: px, close: px };
    series.current.update(bar);
    lastBar.current = bar;
    // keep the same object when nothing moved, so this does not re-render on every tick
    setBarRange((r) =>
      r && bar.low >= r.min && bar.high <= r.max ? r : { min: Math.min(r?.min ?? bar.low, bar.low), max: Math.max(r?.max ?? bar.high, bar.high) },
    );
  }, [entry, tf]);

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
