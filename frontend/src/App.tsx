import { useEffect, useState } from "react";
import { ConnectionBanner } from "@/components/ConnectionBanner";
import { TopBar } from "@/components/TopBar";
import { connectWs } from "@/lib/ws";
import { Home } from "@/pages/Home";
import { cn } from "@/lib/utils";

const PAGES = [
  { id: "home", label: "Home", phase: 1 },
  { id: "live", label: "Live", phase: 3 },
  { id: "journal", label: "Journal", phase: 2 },
  { id: "brain", label: "Brain", phase: 4 },
  { id: "performance", label: "Performance", phase: 4 },
  { id: "backtest", label: "Backtest / Replay", phase: 2 },
  { id: "settings", label: "Settings", phase: 2 },
] as const;

export default function App() {
  const [page, setPage] = useState<(typeof PAGES)[number]["id"]>("home");
  useEffect(() => connectWs(), []);

  return (
    <div className="min-h-screen">
      <TopBar />
      <ConnectionBanner />
      <nav className="flex gap-1 border-b px-4" aria-label="Pages">
        {PAGES.map((p) => (
          <button
            key={p.id}
            disabled={p.phase > 1}
            title={p.phase > 1 ? `Arrives in Phase ${p.phase}` : undefined}
            onClick={() => setPage(p.id)}
            className={cn(
              "border-b-2 px-3 py-2 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40",
              page === p.id ? "border-foreground text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {p.label}
          </button>
        ))}
      </nav>
      <main>{page === "home" && <Home />}</main>
    </div>
  );
}
