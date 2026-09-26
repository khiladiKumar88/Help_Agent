import { TriangleAlert, WifiOff } from "lucide-react";
import { useApp } from "@/store/app";

/** Always-visible warnings: backend disconnected, simulated feed, kill switch, provider errors. */
export function ConnectionBanner() {
  const { conn, reconnectInMs, status } = useApp();
  const banners: { key: string; tone: "loss" | "warning" | "info"; icon: React.ReactNode; text: string }[] = [];

  if (conn !== "open") {
    banners.push({
      key: "conn",
      tone: "loss",
      icon: <WifiOff className="h-4 w-4" />,
      text:
        conn === "connecting"
          ? "Connecting to backend…"
          : `Disconnected from backend — prices and P&L below may be stale.${
              reconnectInMs ? ` Reconnecting in ${Math.round(reconnectInMs / 1000)}s…` : ""
            }`,
    });
  }
  if (status?.kill_switch?.engaged) {
    banners.push({
      key: "kill",
      tone: "loss",
      icon: <TriangleAlert className="h-4 w-4" />,
      text: `Kill switch engaged${status.kill_switch.reason ? `: ${status.kill_switch.reason}` : ""} — all trading halted.`,
    });
  }
  if (status?.provider.provider === "simulated") {
    banners.push({
      key: "sim",
      tone: "info",
      icon: <TriangleAlert className="h-4 w-4" />,
      text: "SIMULATED market data (offline random walk) — prices are not real.",
    });
  } else if (status && !status.provider.connected) {
    banners.push({
      key: "feed",
      tone: "warning",
      icon: <TriangleAlert className="h-4 w-4" />,
      text: `Market data feed is down${status.provider.last_error ? ` (${status.provider.last_error})` : ""}.`,
    });
  }
  if (!banners.length) return null;
  const tone = { loss: "bg-loss/15 text-loss", warning: "bg-warning/15 text-warning", info: "bg-info/15 text-info" };
  return (
    <div role="status" aria-live="polite">
      {banners.map((b) => (
        <div key={b.key} className={`flex items-center gap-2 px-4 py-1.5 text-sm ${tone[b.tone]}`}>
          {b.icon}
          {b.text}
        </div>
      ))}
    </div>
  );
}
