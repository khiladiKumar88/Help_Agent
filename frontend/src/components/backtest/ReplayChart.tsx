import { CandlestickSeries, ColorType, createChart, createSeriesMarkers, type SeriesMarker, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cssColor } from "@/lib/cssColor";
import type { BacktestResult } from "@/lib/types";
import { ACTOR_COLORS as SERIES } from "@/lib/seriesColors";

const sec = (iso: string) => Math.floor(Date.parse(iso) / 1000) as UTCTimestamp;

/** Day chart of a replay with every agent/baseline entry and exit marked. */
export function ReplayChart({ r }: { r: BacktestResult }) {
  const el = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!el.current) return;
    const chart = createChart(el.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: cssColor("--muted-foreground"), attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(255,255,255,0.05)" } },
      timeScale: { timeVisible: true, borderVisible: false },
      rightPriceScale: { borderVisible: false },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: cssColor("--profit"),
      downColor: cssColor("--loss"),
      wickUpColor: cssColor("--profit"),
      wickDownColor: cssColor("--loss"),
      borderVisible: false,
    });
    series.setData(r.candles.map((c) => ({ time: sec(c.t), open: Number(c.o), high: Number(c.h), low: Number(c.l), close: Number(c.c) })));
    const markers: SeriesMarker<UTCTimestamp>[] = [];
    for (const t of r.trades) {
      const who = t.actor === "baseline" ? "B" : "A";
      const color = t.actor === "baseline" ? SERIES.baseline : SERIES.agent;
      if (t.opened_at)
        markers.push({
          time: sec(t.opened_at),
          position: t.direction === "long" ? "belowBar" : "aboveBar",
          shape: t.direction === "long" ? "arrowUp" : "arrowDown",
          color,
          text: `${who} ${t.direction}`,
        });
      if (t.closed_at)
        markers.push({ time: sec(t.closed_at), position: "inBar", shape: "circle", color, text: `${who} exit ${Number(t.net_pnl) >= 0 ? "+" : ""}${Number(t.net_pnl).toFixed(2)}` });
    }
    markers.sort((a, b) => a.time - b.time);
    createSeriesMarkers(series, markers);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [r]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-foreground">Replay day</CardTitle>
        <span className="text-xs text-muted-foreground">
          <span className="mr-1 inline-block h-0.5 w-3 align-middle" style={{ background: SERIES.agent }} />A = agent ·{" "}
          <span className="mr-1 inline-block h-0.5 w-3 align-middle" style={{ background: SERIES.baseline }} />B = random baseline · arrows = entries, dots = exits
        </span>
      </CardHeader>
      <CardContent>
        <div ref={el} className="h-80 w-full" data-testid="replay-chart" />
      </CardContent>
    </Card>
  );
}
