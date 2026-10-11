import { barExtent, bucketTime, candleToBar, sortedBars, tickBar, utcSeconds } from "@/lib/chartBars";

const candle = (over: Partial<Record<string, string>> = {}) => ({
  ts_open: "2026-01-05T10:05:00.000Z",
  open: "65000",
  high: "65100",
  low: "64900",
  close: "65050",
  ...over,
});

describe("utcSeconds / bucketTime", () => {
  it("gives whole UTC seconds, never milliseconds and never an object", () => {
    const t = utcSeconds("2026-01-05T10:05:30.750Z");
    expect(t).toBe(Date.UTC(2026, 0, 5, 10, 5, 30) / 1000);
    expect(Number.isInteger(t)).toBe(true);
  });

  it("floors a timestamp into its timeframe bucket", () => {
    expect(bucketTime("2026-01-05T10:58:30.000Z", 900)).toBe(Date.UTC(2026, 0, 5, 10, 45) / 1000);
    expect(bucketTime("2026-01-05T10:58:30.000Z", 300)).toBe(Date.UTC(2026, 0, 5, 10, 55) / 1000);
    expect(bucketTime("2026-01-05T10:58:30.000Z", 60)).toBe(Date.UTC(2026, 0, 5, 10, 58) / 1000);
  });

  it("returns null for an unparseable time or a nonsense timeframe", () => {
    expect(bucketTime("not a date", 60)).toBeNull();
    expect(bucketTime("2026-01-05T10:00:00Z", 0)).toBeNull();
  });
});

describe("candleToBar / sortedBars", () => {
  it("converts decimal strings to numbers and the time to seconds", () => {
    expect(candleToBar(candle())).toEqual({
      time: Date.UTC(2026, 0, 5, 10, 5) / 1000,
      open: 65000,
      high: 65100,
      low: 64900,
      close: 65050,
    });
  });

  it("drops a candle with an unusable time or price", () => {
    expect(candleToBar(null)).toBeNull();
    expect(candleToBar(candle({ ts_open: "" }))).toBeNull();
    expect(candleToBar(candle({ close: "n/a" }))).toBeNull();
  });

  it("sorts ascending and drops nulls and duplicate times (setData requires both)", () => {
    const a = candleToBar(candle({ ts_open: "2026-01-05T10:05:00Z" }))!;
    const b = candleToBar(candle({ ts_open: "2026-01-05T10:10:00Z" }))!;
    expect(sortedBars([b, null, a, { ...a, close: 1 }]).map((x) => x.time)).toEqual([a.time, b.time]);
  });
});

describe("tickBar", () => {
  const prev = { time: 1000, open: 10, high: 12, low: 9, close: 11 };

  it("extends the bar the tick belongs to", () => {
    expect(tickBar(prev, 1000, 15)).toEqual({ time: 1000, open: 10, high: 15, low: 9, close: 15 });
    expect(tickBar(prev, 1000, 5)).toEqual({ time: 1000, open: 10, high: 12, low: 5, close: 5 });
  });

  it("opens the next bar at the previous close", () => {
    expect(tickBar(prev, 1060, 14)).toEqual({ time: 1060, open: 11, high: 14, low: 14, close: 14 });
  });

  it("starts from the tick itself when there is no previous bar", () => {
    expect(tickBar(null, 1000, 7)).toEqual({ time: 1000, open: 7, high: 7, low: 7, close: 7 });
  });

  it("IGNORES a tick older than the last bar — update() throws on one", () => {
    expect(tickBar(prev, 940, 14)).toBeNull();
  });

  it("ignores an unusable time or price", () => {
    expect(tickBar(prev, null, 14)).toBeNull();
    expect(tickBar(prev, 1060, Number.NaN)).toBeNull();
  });
});

it("barExtent spans the low/high of the bars, or null when there are none", () => {
  expect(barExtent([])).toBeNull();
  expect(barExtent([{ high: 5, low: 1 }, { high: 9, low: 4 }])).toEqual({ min: 1, max: 9 });
});
