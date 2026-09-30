// Fitting the price scale around a position's levels. Pure maths so it can be unit-tested
// without a chart: PriceChart feeds the result to lightweight-charts' autoscale provider.

export type LevelKind = "entry" | "sl" | "target";

export interface PriceLevel {
  kind: LevelKind;
  /** Short label shown on the price line and on an out-of-range marker. */
  label: string;
  price: number;
}

export interface FittedRange {
  /** Range the price scale should cover (bars, plus every level that fits). */
  min: number;
  max: number;
  /** Levels the fitted range covers — drawn as ordinary price lines. */
  included: PriceLevel[];
  /** Levels too far away to fit; shown as edge markers instead of squashing the candles. */
  below: PriceLevel[];
  above: PriceLevel[];
}

/**
 * Grow [barMin, barMax] so it also covers `levels`.
 *
 * A stop or target far from the current price would flatten the candles into a line, so a
 * level is only taken in while it stays within `maxExpansion` times the bar range of it.
 * Anything further out is reported in `below` / `above` for an edge marker.
 */
export function fitPriceRange(
  barMin: number | null,
  barMax: number | null,
  levels: PriceLevel[],
  maxExpansion = 1,
): FittedRange | null {
  if (barMin === null || barMax === null || !Number.isFinite(barMin) || !Number.isFinite(barMax)) return null;
  const lo = Math.min(barMin, barMax);
  const hi = Math.max(barMin, barMax);
  // a flat window (one bar, or no movement) still needs a non-zero span to budget against
  const span = hi - lo || Math.abs(hi) * 0.002 || 1;
  const budgetLo = lo - span * maxExpansion;
  const budgetHi = hi + span * maxExpansion;

  const out: FittedRange = { min: lo, max: hi, included: [], below: [], above: [] };
  for (const l of levels) {
    if (!Number.isFinite(l.price)) continue;
    if (l.price < budgetLo) out.below.push(l);
    else if (l.price > budgetHi) out.above.push(l);
    else {
      out.included.push(l);
      out.min = Math.min(out.min, l.price);
      out.max = Math.max(out.max, l.price);
    }
  }
  out.below.sort((a, b) => b.price - a.price); // nearest the window first
  out.above.sort((a, b) => a.price - b.price);
  return out;
}

/** Levels worth drawing for one open position. */
export function positionLevels(p: {
  direction: string;
  avg_entry: string | null;
  current_sl: string;
  target: string | null;
}): PriceLevel[] {
  const side = p.direction === "long" ? "Long" : "Short";
  const levels: PriceLevel[] = [];
  const entry = Number(p.avg_entry);
  if (p.avg_entry && Number.isFinite(entry)) levels.push({ kind: "entry", label: `${side} entry`, price: entry });
  const sl = Number(p.current_sl);
  if (Number.isFinite(sl)) levels.push({ kind: "sl", label: "SL", price: sl });
  const target = Number(p.target);
  if (p.target && Number.isFinite(target)) levels.push({ kind: "target", label: "Target", price: target });
  return levels;
}
