import { useApp } from "@/store/app";
import type { BookSummary, Candle, RiskDecisionRow, SystemStatus, Tick, TradeView } from "./types";

type Msg = { type: string; data: unknown };

/** Route one server message into the store. Exported for tests. */
export function handleMessage(msg: Msg): void {
  const s = useApp.getState();
  s.touch();
  switch (msg.type) {
    case "status":
      s.setStatus(msg.data as SystemStatus);
      break;
    case "tick":
      s.onTick(msg.data as Tick);
      break;
    case "candle":
      s.onCandle(msg.data as Candle);
      break;
    case "book":
      s.setBook(msg.data as BookSummary);
      break;
    case "trade":
      s.onTrade(msg.data as TradeView);
      break;
    case "signal":
      s.onSignal();
      break;
    case "risk_decision": {
      const d = msg.data as RiskDecisionRow & { book_id: string };
      s.pushRisk({ ...d, id: `${Date.now()}-${Math.random()}`, ts: new Date().toISOString() });
      break;
    }
    default:
      break;
  }
}

/** WebSocket with exponential-backoff reconnect and keepalive pings. Returns a stop function. */
export function connectWs(url = defaultWsUrl()): () => void {
  let ws: WebSocket | null = null;
  let stopped = false;
  let attempt = 0;
  let ping: ReturnType<typeof setInterval> | undefined;
  let retry: ReturnType<typeof setTimeout> | undefined;

  const open = () => {
    if (stopped) return;
    useApp.getState().setConn("connecting");
    ws = new WebSocket(url);
    ws.onopen = () => {
      attempt = 0;
      useApp.getState().setConn("open");
      ping = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ type: "ping" })), 10_000);
    };
    ws.onmessage = (ev) => {
      try {
        handleMessage(JSON.parse(ev.data as string) as Msg);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      clearInterval(ping);
      if (stopped) return;
      const delay = Math.min(30_000, 1000 * 2 ** attempt++);
      useApp.getState().setConn("closed", delay);
      retry = setTimeout(open, delay);
    };
    ws.onerror = () => ws?.close();
  };

  open();
  return () => {
    stopped = true;
    clearInterval(ping);
    clearTimeout(retry);
    ws?.close();
  };
}

function defaultWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws`;
}
