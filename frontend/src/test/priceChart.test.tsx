import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { ErrorBoundary } from "@/components/ErrorBoundary";
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

/** Closed candles on an arbitrary timeframe, starting 10:00 UTC. */
const barsFor = (tf: string, secs: number, n: number): Candle[] =>
  Array.from({ length: n }, (_, i) => ({
    instrument_id: "i1",
    timeframe: tf,
    ts_open: new Date(Date.UTC(2026, 0, 5, 10, 0) + i * secs * 1000).toISOString(),
    open: "65000",
    high: "65100",
    low: "64900",
    close: "65000",
    volume: "1",
    closed: true,
  }));

const pushTick = (iso: string, ltp = "65010") =>
  act(() => {
    useApp.getState().onTick({ instrument_id: "i1", ts: iso, ltp, bid: null, ask: null, volume: null, source: "test" });
  });

describe("PriceChart timeframe switching", () => {
  beforeEach(() => {
    vi.spyOn(api, "candles").mockImplementation(async (_id, tf) =>
      tf === "15m"
        ? { closed: barsFor("15m", 900, 6), forming: null } // last bar opens 11:15
        : { closed: barsFor("5m", 300, 12), forming: null }, // last bar opens 10:55
    );
  });

  it("switches 5m -> 15m while ticks are arriving without crashing the chart", async () => {
    const user = userEvent.setup();
    render(
      <ErrorBoundary name="Price chart">
        <PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[]} />
      </ErrorBoundary>,
    );
    await user.click(screen.getByRole("radio", { name: "5m" }));
    await waitFor(() => expect(api.candles).toHaveBeenCalledWith("i1", "5m", 300));
    await pushTick("2026-01-05T10:58:00.000Z"); // inside the last 5m bar (10:55)

    await user.click(screen.getByRole("radio", { name: "15m" }));
    // These floor into the 10:45 15m bucket — OLDER than the 5m bar still on screen a moment ago.
    // Before the fix this called update() with a stale bar and threw "Cannot update oldest data".
    await pushTick("2026-01-05T10:58:30.000Z");
    await pushTick("2026-01-05T10:59:00.000Z");

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByTestId("price-chart")).toBeInTheDocument();
  });

  it("ignores ticks that arrive before the new timeframe's history has loaded", async () => {
    const user = userEvent.setup();
    let release: ((v: { closed: Candle[]; forming: Candle | null }) => void) | undefined;
    vi.spyOn(api, "candles").mockImplementation((_id, tf) =>
      tf === "1h"
        ? new Promise((res) => {
            release = res;
          })
        : Promise.resolve({ closed: barsFor("5m", 300, 12), forming: null }),
    );
    render(
      <ErrorBoundary name="Price chart">
        <PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[]} />
      </ErrorBoundary>,
    );
    await user.click(screen.getByRole("radio", { name: "5m" }));
    await waitFor(() => expect(api.candles).toHaveBeenCalledWith("i1", "5m", 300));
    await user.click(screen.getByRole("radio", { name: "1h" }));

    await pushTick("2026-01-05T10:58:30.000Z"); // 1h history still in flight
    await act(async () => {
      release?.({ closed: barsFor("1h", 3600, 4), forming: null }); // last bar opens 13:00
    });
    await pushTick("2026-01-05T13:30:00.000Z"); // inside the newest 1h bar

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByTestId("price-chart")).toBeInTheDocument();
  });

  it("resets the series when the instrument changes", async () => {
    const { rerender } = render(<PriceChart instrumentId="i1" symbol="BTC/USDT:USDT" positions={[]} />);
    await waitFor(() => expect(api.candles).toHaveBeenCalledWith("i1", "1m", 300));
    rerender(<PriceChart instrumentId="i2" symbol="ETH/USDT:USDT" positions={[]} />);
    await waitFor(() => expect(api.candles).toHaveBeenCalledWith("i2", "1m", 300));
    await pushTick("2026-01-05T10:58:30.000Z"); // a tick for the OLD instrument
    expect(screen.getByTestId("price-chart")).toBeInTheDocument();
  });
});
