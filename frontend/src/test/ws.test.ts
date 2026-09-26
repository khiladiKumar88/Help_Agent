import { handleMessage } from "@/lib/ws";
import { useApp } from "@/store/app";

describe("websocket message routing", () => {
  it("updates ticks, books, trades and risk feed", () => {
    handleMessage({ type: "tick", data: { instrument_id: "i1", ts: "2026-01-01T00:00:00Z", ltp: "10", bid: "9", ask: "11", volume: null, source: "t" } });
    expect(useApp.getState().ticks.i1?.tick.ltp).toBe("10");
    handleMessage({ type: "book", data: { book_id: "b1", balance: "1" } });
    expect(useApp.getState().books.b1?.balance).toBe("1");
    const v = useApp.getState().tradeVersion;
    handleMessage({ type: "trade", data: { id: "t" } });
    expect(useApp.getState().tradeVersion).toBe(v + 1);
    handleMessage({ type: "risk_decision", data: { approved: false, failed_rule_id: "R006_MAX_RISK_PER_TRADE", message: "too big", actor: "human", instrument_id: "i1" } });
    expect(useApp.getState().riskFeed[0]?.failed_rule_id).toBe("R006_MAX_RISK_PER_TRADE");
    handleMessage({ type: "candle", data: { instrument_id: "i1", timeframe: "1m", close: "10" } });
    expect(useApp.getState().lastCandle.i1).toBeTruthy();
    handleMessage({ type: "unknown", data: {} });
    expect(useApp.getState().lastMessageAt).not.toBeNull();
  });
});
