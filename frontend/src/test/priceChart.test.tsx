import { render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { PriceChart } from "@/components/PriceChart";
import { api } from "@/lib/api";
import type { Candle, TradeView } from "@/lib/types";
import { useApp } from "@/store/app";

/** Closed 1m candles spanning 64,900–65,100 — a 200-wide window. */
const candles = (): Candle[] =>
  [0, 1, 2].map((i) => ({
    instrument_id: "i1",
    timeframe: "1m",
    ts_open: new Date(Date.UTC(2026, 0, 5, 10, i)).toISOString(),
    open: "65000",
    high: "65100",
    low: "64900",
    close: "65000",
    volume: "1",
    closed: true,
  }));

const position = (over: Partial<TradeView> = {}): TradeView =>
  ({
    id: "t1",
    instrument_id: "i1",
    symbol: "BTC/USDT:USDT",
    direction: "long",
    status: "open",
    qty: "0.005",
    avg_entry: "65013",
    current_sl: "64950",
    target: "65080",
    ...over,
  }) as TradeView;

beforeEach(() => {
  vi.restoreAllMocks();
  useApp.setState({ conn: "open", status: null, ticks: {} });
  vi.spyOn(api, "candles").mockResolvedValue({ closed: candles(), forming: null });
});

describe("PriceChart price-scale fitting", () => {
  it("renders the chart", async () => {
    render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[]} />);
    expect(screen.getByTestId("price-chart")).toBeInTheDocument();
    await waitFor(() => expect(api.candles).toHaveBeenCalled());
  });

  it("says nothing when the SL and target sit inside the candles", async () => {
    render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[position()]} />);
    await waitFor(() => expect(api.candles).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("price-scale-notice")).not.toBeInTheDocument());
  });

  it("keeps quiet for levels just outside the candles — the scale stretches to them", async () => {
    // bar window is 64,900..65,100 (span 200), so the budget reaches 64,700..65,300
    const p = position({ current_sl: "64800", target: "65250" });
    render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[p]} />);
    await waitFor(() => expect(api.candles).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("price-scale-notice")).not.toBeInTheDocument());
  });

  it("marks an SL that is too far below to fit on the scale", async () => {
    const p = position({ current_sl: "60000", target: "65080" });
    render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[p]} />);
    const notice = await screen.findByTestId("price-scale-notice");
    expect(notice).toHaveTextContent("SL 60,000.0 below");
    expect(notice).not.toHaveTextContent("Target");
  });

  it("marks a target that is too far above to fit on the scale", async () => {
    const p = position({ direction: "short", current_sl: "65050", target: "90000" });
    render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[p]} />);
    const notice = await screen.findByTestId("price-scale-notice");
    expect(notice).toHaveTextContent("Target 90,000.0 above");
    expect(notice).not.toHaveTextContent("SL");
  });

  it("ignores positions on other instruments", async () => {
    const p = position({ instrument_id: "other", current_sl: "60000" });
    render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[p]} />);
    await waitFor(() => expect(api.candles).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("price-scale-notice")).not.toBeInTheDocument());
  });

  it("says nothing when there are no candles to scale against", async () => {
    vi.spyOn(api, "candles").mockResolvedValue({ closed: [], forming: null });
    render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[position({ current_sl: "1" })]} />);
    await waitFor(() => expect(api.candles).toHaveBeenCalled());
    expect(screen.queryByTestId("price-scale-notice")).not.toBeInTheDocument();
  });
});
