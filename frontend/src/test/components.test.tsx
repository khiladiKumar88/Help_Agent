import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { ConnectionBanner } from "@/components/ConnectionBanner";
import { OrderPanel } from "@/components/OrderPanel";
import { KillSwitch, TopBar } from "@/components/TopBar";
import { api } from "@/lib/api";
import type { BookSummary, Instrument, Preview, SystemStatus } from "@/lib/types";
import { useApp } from "@/store/app";

const status: SystemStatus = {
  server_time: "",
  replay: false,
  kill_switch: { engaged: false },
  provider: { provider: "ccxt", exchange: "binanceusdm", connected: true },
  data: {},
  stale_after_seconds: 10,
  books: { "crypto-futures-intraday": { status: "running", mode: "manual" } },
  llm: { provider: null, calls_today: 0, daily_budget: null },
  secrets: {},
};

const book = {
  book_id: "crypto-futures-intraday", currency: "USDT", mode: "manual", market: "crypto", segment: "futures",
  style: "intraday", positions: [], instruments: ["i1"],
} as unknown as BookSummary;
const inst = { id: "i1", symbol: "BTC/USDT:USDT", exchange: "binanceusdm", qty_step: "0.001" } as Instrument;

beforeEach(() => {
  vi.restoreAllMocks();
  useApp.setState({ conn: "open", status, ticks: {}, books: {} });
});

describe("TopBar", () => {
  it("shows running status and disables phase-3 modes", () => {
    render(<TopBar />);
    expect(screen.getByLabelText(/Agent status: Running/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Co-pilot" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Manual" })).toBeEnabled();
  });
  it("shows backend offline when the socket is down", () => {
    useApp.setState({ conn: "closed" });
    render(<TopBar />);
    expect(screen.getByLabelText(/Backend offline/)).toBeInTheDocument();
  });
  it("marks unconfigured books", async () => {
    render(<TopBar />);
    await userEvent.click(screen.getByRole("radio", { name: "India" }));
    expect(screen.getByLabelText(/Not configured/)).toBeInTheDocument();
  });
});

describe("KillSwitch", () => {
  it("requires confirmation before calling the API", async () => {
    const spy = vi.spyOn(api, "killSwitch").mockResolvedValue({ engaged: true, closed: 1, cancelled: 0 });
    render(<KillSwitch engaged={false} />);
    await userEvent.click(screen.getByRole("button", { name: /KILL SWITCH/ }));
    expect(spy).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /Yes, close everything/ }));
    expect(spy).toHaveBeenCalledWith("manual kill switch");
  });
  it("offers release when engaged", async () => {
    const spy = vi.spyOn(api, "releaseKillSwitch").mockResolvedValue({ engaged: false });
    render(<KillSwitch engaged />);
    await userEvent.click(screen.getByRole("button", { name: /Release kill switch/ }));
    await userEvent.click(screen.getByRole("button", { name: "Release" }));
    expect(spy).toHaveBeenCalled();
  });
});

describe("ConnectionBanner", () => {
  it("warns when disconnected", () => {
    useApp.setState({ conn: "closed", reconnectInMs: 4000 });
    render(<ConnectionBanner />);
    expect(screen.getByText(/Disconnected from backend/)).toHaveTextContent("Reconnecting in 4s");
  });
  it("labels simulated data", () => {
    useApp.setState({ status: { ...status, provider: { ...status.provider, provider: "simulated" } } });
    render(<ConnectionBanner />);
    expect(screen.getByText(/SIMULATED market data/)).toBeInTheDocument();
  });
  it("renders nothing when healthy", () => {
    const { container } = render(<ConnectionBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("OrderPanel", () => {
  const preview = (approved: boolean): Preview => ({
    decision: {
      approved,
      failed_rule_id: approved ? null : "R006_MAX_RISK_PER_TRADE",
      message: approved ? "ok" : "risk 21.00 exceeds max 10.00 USDT",
      results: [],
      risk_amount: "5",
      est_charges: "0.3",
    },
    est_entry: "65000", notional: "325", margin_required: "108", available_margin: "1000", risk_amount: "5.3",
    reward_amount: "9", reward_risk: "1.70", est_round_trip_charges: "0.32", max_risk_allowed: "10",
    max_qty_by_risk: "0.009", stale: false,
  });

  const freshTick = () =>
    useApp.setState({
      ticks: { i1: { tick: { instrument_id: "i1", ts: new Date().toISOString(), ltp: "65000", bid: null, ask: null, volume: null, source: "t" }, receivedAt: Date.now() } },
    });

  it("shows the blocking rule and keeps the button disabled", async () => {
    freshTick();
    vi.spyOn(api, "preview").mockResolvedValue(preview(false));
    render(<OrderPanel book={book} instruments={[inst]} instrumentId="i1" onInstrument={() => undefined} />);
    await userEvent.type(screen.getByLabelText(/Qty/), "0.02");
    await waitFor(() => expect(screen.getByTestId("risk-verdict")).toHaveTextContent("R006_MAX_RISK_PER_TRADE"));
    expect(screen.getByRole("button", { name: /Place paper BUY/ })).toBeDisabled();
  });

  it("places an approved order and offers max-by-risk sizing", async () => {
    freshTick();
    vi.spyOn(api, "preview").mockResolvedValue(preview(true));
    const place = vi.spyOn(api, "place").mockResolvedValue({ approved: true, decision: preview(true).decision, trade: null });
    render(<OrderPanel book={book} instruments={[inst]} instrumentId="i1" onInstrument={() => undefined} />);
    await userEvent.type(screen.getByLabelText(/Qty/), "0.005");
    await userEvent.type(screen.getByLabelText(/Stop-loss/), "64000");
    await waitFor(() => expect(screen.getByTestId("risk-verdict")).toHaveTextContent("All risk rules pass"));
    await userEvent.click(screen.getByRole("button", { name: /max by risk/ }));
    expect(screen.getByLabelText(/Qty/)).toHaveValue("0.009");
    await waitFor(() => expect(screen.getByRole("button", { name: /Place paper BUY/ })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: /Place paper BUY/ }));
    expect(place).toHaveBeenCalledWith(expect.objectContaining({ instrument_id: "i1", direction: "long", qty: "0.009", stop_loss: "64000" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("accepted");
  });

  it("refuses to trade on stale prices", async () => {
    vi.spyOn(api, "preview").mockResolvedValue(preview(true));
    render(<OrderPanel book={book} instruments={[inst]} instrumentId="i1" onInstrument={() => undefined} />);
    await userEvent.type(screen.getByLabelText(/Qty/), "0.005");
    expect(screen.getByRole("button", { name: /Price stale/ })).toBeDisabled();
  });
});
