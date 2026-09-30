import { render, screen } from "@testing-library/react";
import { PriceScaleNotice } from "@/components/PriceScaleNotice";
import { fitPriceRange, type PriceLevel, positionLevels } from "@/lib/priceRange";
import type { TradeView } from "@/lib/types";

const lv = (label: string, price: number, kind: PriceLevel["kind"] = "sl"): PriceLevel => ({ kind, label, price });

describe("fitPriceRange", () => {
  it("returns null without a bar range", () => {
    expect(fitPriceRange(null, null, [lv("SL", 1)])).toBeNull();
    expect(fitPriceRange(NaN, 10, [])).toBeNull();
  });

  it("keeps the bar range when there are no levels", () => {
    const f = fitPriceRange(100, 110, [])!;
    expect([f.min, f.max]).toEqual([100, 110]);
    expect(f.included).toEqual([]);
  });

  it("grows the range to cover an SL and a target just outside the candles", () => {
    const f = fitPriceRange(100, 110, [lv("SL", 96), lv("Target", 118, "target")])!;
    expect([f.min, f.max]).toEqual([96, 118]);
    expect(f.included.map((l) => l.label)).toEqual(["SL", "Target"]);
    expect(f.below).toEqual([]);
    expect(f.above).toEqual([]);
  });

  it("leaves levels inside the candles alone", () => {
    const f = fitPriceRange(100, 110, [lv("Long entry", 105, "entry")])!;
    expect([f.min, f.max]).toEqual([100, 110]);
    expect(f.included).toHaveLength(1);
  });

  it("refuses to squash the candles for a far-away level and reports it instead", () => {
    // bar span is 10, so the budget reaches 90..120; 50 and 200 are well outside it
    const f = fitPriceRange(100, 110, [lv("SL", 50), lv("Target", 200, "target")])!;
    expect([f.min, f.max]).toEqual([100, 110]);
    expect(f.below.map((l) => l.label)).toEqual(["SL"]);
    expect(f.above.map((l) => l.label)).toEqual(["Target"]);
  });

  it("takes in a level exactly at the edge of the budget", () => {
    const f = fitPriceRange(100, 110, [lv("SL", 90)])!; // 100 - 10*1
    expect(f.min).toBe(90);
    expect(f.below).toEqual([]);
  });

  it("honours a wider expansion budget", () => {
    expect(fitPriceRange(100, 110, [lv("SL", 85)], 1)!.below).toHaveLength(1);
    expect(fitPriceRange(100, 110, [lv("SL", 85)], 2)!.min).toBe(85);
  });

  it("still budgets when every bar is flat", () => {
    const f = fitPriceRange(100, 100, [lv("SL", 99.9)])!;
    expect(f.min).toBe(99.9); // span falls back to 0.2% of the price
    expect(fitPriceRange(100, 100, [lv("SL", 50)])!.below).toHaveLength(1);
  });

  it("orders out-of-range levels nearest-first and tolerates reversed bounds", () => {
    const f = fitPriceRange(110, 100, [lv("A", 10), lv("B", 50), lv("C", 500), lv("D", 300)])!;
    expect(f.below.map((l) => l.label)).toEqual(["B", "A"]);
    expect(f.above.map((l) => l.label)).toEqual(["D", "C"]);
  });

  it("ignores unparseable prices", () => {
    const f = fitPriceRange(100, 110, [lv("bad", NaN), lv("SL", 105)])!;
    expect(f.included.map((l) => l.label)).toEqual(["SL"]);
  });
});

describe("positionLevels", () => {
  const pos = (over: Partial<TradeView> = {}) =>
    ({ direction: "long", avg_entry: "65013", current_sl: "64000", target: "67000", ...over }) as TradeView;

  it("returns entry, SL and target", () => {
    expect(positionLevels(pos())).toEqual([
      { kind: "entry", label: "Long entry", price: 65013 },
      { kind: "sl", label: "SL", price: 64000 },
      { kind: "target", label: "Target", price: 67000 },
    ]);
  });

  it("labels a short and skips a missing entry or target", () => {
    const l = positionLevels(pos({ direction: "short", avg_entry: null, target: null }));
    expect(l.map((x) => x.label)).toEqual(["SL"]);
    expect(positionLevels(pos({ direction: "short" }))[0]!.label).toBe("Short entry");
  });
});

describe("PriceScaleNotice", () => {
  it("renders nothing when everything fits", () => {
    const { container } = render(<PriceScaleNotice fit={fitPriceRange(100, 110, [lv("SL", 105)])} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing without a fit", () => {
    const { container } = render(<PriceScaleNotice fit={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("marks levels that fell off the top and bottom of the scale", () => {
    render(<PriceScaleNotice fit={fitPriceRange(100, 110, [lv("SL", 50), lv("Target", 200, "target")])} />);
    const notice = screen.getByTestId("price-scale-notice");
    expect(notice).toHaveTextContent("SL 50.0 below");
    expect(notice).toHaveTextContent("Target 200.0 above");
  });
});
