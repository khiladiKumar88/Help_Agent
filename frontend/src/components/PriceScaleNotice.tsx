import { ArrowDown, ArrowUp } from "lucide-react";
import type { FittedRange } from "@/lib/priceRange";
import { fmtPrice } from "@/lib/utils";

/**
 * Edge markers for the levels the chart could not fit on its price scale. Without these, a stop
 * far from the current price would silently vanish off the chart.
 */
export function PriceScaleNotice({ fit }: { fit: FittedRange | null }) {
  if (!fit || (!fit.below.length && !fit.above.length)) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground" data-testid="price-scale-notice">
      <span>Off the chart:</span>
      {fit.above.map((l) => (
        <span key={`a-${l.label}`} className="inline-flex items-center gap-1 rounded bg-muted/60 px-1.5 py-0.5 tabular-nums">
          <ArrowUp className="h-3 w-3" aria-hidden />
          {l.label} {fmtPrice(String(l.price))} above
        </span>
      ))}
      {fit.below.map((l) => (
        <span key={`b-${l.label}`} className="inline-flex items-center gap-1 rounded bg-muted/60 px-1.5 py-0.5 tabular-nums">
          <ArrowDown className="h-3 w-3" aria-hidden />
          {l.label} {fmtPrice(String(l.price))} below
        </span>
      ))}
    </div>
  );
}
