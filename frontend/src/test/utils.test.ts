import { ageSeconds, bookId, fmtAxisTime, timeTicks, fmtMoney, fmtNum, fmtPrice, fmtSigned, num, pnlClass, shortSymbol, slippageNote } from "@/lib/utils";
import { isPriceStale } from "@/store/app";

describe("formatters", () => {
  it("parses decimals for display only", () => {
    expect(num("1.50")).toBe(1.5);
    expect(num(null)).toBeNull();
    expect(num("abc")).toBeNull();
  });
  it("formats numbers, signs and money", () => {
    expect(fmtNum("1234.5", 2)).toBe("1,234.50");
    expect(fmtSigned("-5.452")).toBe("−5.45");
    expect(fmtSigned("2")).toBe("+2.00");
    expect(fmtMoney("1000", "USDT")).toBe("1,000.00 USDT");
    expect(fmtMoney("20000", "INR", 0)).toBe("₹20,000");
    expect(fmtMoney(null, "USDT")).toBe("—");
    expect(fmtPrice("65013.10")).toBe("65,013.1");
    expect(fmtPrice("65000")).toBe("65,000.0");
  });
  it("colours P&L", () => {
    expect(pnlClass("1")).toBe("text-profit");
    expect(pnlClass("-1")).toBe("text-loss");
    expect(pnlClass("0")).toBe("text-muted-foreground");
  });
  it("builds ids and labels", () => {
    expect(bookId("crypto", "futures", "intraday")).toBe("crypto-futures-intraday");
    expect(shortSymbol("BTC/USDT:USDT")).toBe("BTC/USDT perp");
    expect(shortSymbol("BTC/USDT")).toBe("BTC/USDT");
    expect(ageSeconds("2026-01-01T00:00:10Z", Date.parse("2026-01-01T00:00:20Z"))).toBe(10);
  });
});

describe("stale price detection", () => {
  const entry = { tick: { instrument_id: "x", ts: "", ltp: "1", bid: null, ask: null, volume: null, source: "t" }, receivedAt: 1_000 };
  it("is stale when disconnected, missing, or old", () => {
    expect(isPriceStale(undefined, "open", 10, 2_000)).toBe(true);
    expect(isPriceStale(entry, "closed", 10, 2_000)).toBe(true);
    expect(isPriceStale(entry, "open", 10, 5_000)).toBe(false);
    expect(isPriceStale(entry, "open", 10, 12_000)).toBe(true);
  });
});

describe("time axis helpers", () => {
  it("returns unique evenly spaced ticks", () => {
    expect(timeTicks([0, 100, 100, 100], 5)).toEqual([0, 25, 50, 75, 100]);
    expect(timeTicks([5, 5])).toEqual([5]);
    expect(timeTicks([])).toEqual([]);
    expect(fmtAxisTime(Date.UTC(2026, 0, 5), 10 * 86_400_000)).toMatch(/Jan/);
  });
});

describe("slippageNote", () => {
  const s = (over: Partial<Parameters<typeof slippageNote>[0]> = {}) =>
    slippageNote({ model: "bps", bps: "2", extra_ticks: 0, est_spread_bps: "2", reference_kind: "ask", ...over });

  it("names the bps and the side of the book it is measured from", () => {
    expect(s()).toBe("2 bps worse than the ask");
    expect(s({ reference_kind: "bid" })).toBe("2 bps worse than the bid");
    expect(s({ bps: "10.5" })).toBe("10.5 bps worse than the ask");
  });

  it("falls back to the estimated spread when there is no quote", () => {
    expect(s({ reference_kind: "ltp_half_spread", est_spread_bps: "4" })).toBe("2 bps worse than LTP ± ½ spread (4 bps)");
  });

  it("counts ticks for the spread model", () => {
    expect(s({ model: "spread", extra_ticks: 2 })).toBe("2 ticks worse than the ask");
    expect(s({ model: "spread", extra_ticks: 1 })).toBe("1 tick worse than the ask");
  });
});
