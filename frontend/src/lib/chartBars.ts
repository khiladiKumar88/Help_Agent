/** Candle bookkeeping for the live price chart, as pure functions.
 *
 * `lightweight-charts` is strict about time: `series.update()` throws
 * "Cannot update oldest data" for a bar older than the last one it holds, and it expects a
 * UTC timestamp in whole seconds — never a Date, never a string, never a fractional number.
 * Both rules are enforced here rather than inside the chart effects, so they are testable
 * without mounting a chart (see `test/chartBars.test.ts`).
 */

/** A candle as lightweight-charts wants it: `time` is UTC *seconds*, not milliseconds. */
export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

/** The timeframes the Live chart offers, and their length in seconds. */
export const TF_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 } as const;
export type Tf = keyof typeof TF_SECONDS;

/** Whole UTC seconds for an ISO timestamp, or null when it does not parse. */
export function utcSeconds(iso: string): number | null {
  const ms = new Date(iso).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

/** Floor an ISO timestamp to the start of its timeframe bucket, in whole UTC seconds. */
export function bucketTime(iso: string, tfSeconds: number): number | null {
  const secs = utcSeconds(iso);
  if (secs === null || !Number.isFinite(tfSeconds) || tfSeconds <= 0) return null;
  return Math.floor(secs / tfSeconds) * tfSeconds;
}

/** A closed/forming candle from the API as a chart bar, or null if its time or prices are unusable. */
export function candleToBar(
  c: { ts_open: string; open: string; high: string; low: string; close: string } | null | undefined,
): Bar | null {
  if (!c) return null;
  const time = utcSeconds(c.ts_open);
  const [open, high, low, close] = [c.open, c.high, c.low, c.close].map(Number);
  if (time === null || ![open, high, low, close].every(Number.isFinite)) return null;
  return { time, open: open!, high: high!, low: low!, close: close! };
}

/** Drop anything unusable and keep the bars strictly ascending — `setData` requires both. */
export function sortedBars(bars: (Bar | null)[]): Bar[] {
  const clean = bars.filter((b): b is Bar => b !== null).sort((a, b) => a.time - b.time);
  return clean.filter((b, i) => i === 0 || b.time > clean[i - 1]!.time);
}

/**
 * The bar a tick produces, or null when the tick must be ignored.
 *
 * Null means "do not call update()": the tick is unusable, or it belongs to a bucket the chart
 * has already moved past. That happens routinely right after a timeframe switch — a 5m tick
 * floored into a 15m bucket can land *before* the newest bar on screen — and calling update()
 * with it is exactly what throws "Cannot update oldest data".
 */
export function tickBar(prev: Bar | null, time: number | null, price: number): Bar | null {
  if (time === null || !Number.isFinite(time) || !Number.isFinite(price)) return null;
  if (prev === null) return { time, open: price, high: price, low: price, close: price };
  if (time < prev.time) return null; // a late tick for a bar we have already left
  if (time === prev.time) return { ...prev, high: Math.max(prev.high, price), low: Math.min(prev.low, price), close: price };
  return { time, open: prev.close, high: price, low: price, close: price };
}

export const barExtent = (bars: { high: number; low: number }[]) =>
  bars.length ? { min: Math.min(...bars.map((b) => b.low)), max: Math.max(...bars.map((b) => b.high)) } : null;
