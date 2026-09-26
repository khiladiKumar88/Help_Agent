import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Parse a backend decimal string for DISPLAY only (never for money maths). */
export function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function fmtNum(v: string | number | null | undefined, digits = 2): string {
  const n = num(v);
  if (n === null) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Price with as many decimals as the value carries (up to 8). */
export function fmtPrice(v: string | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const decimals = v.includes(".") ? Math.min(v.split(".")[1]!.replace(/0+$/, "").length, 8) : 0;
  return fmtNum(v, Math.max(decimals, 1));
}

export function fmtSigned(v: string | number | null | undefined, digits = 2): string {
  const n = num(v);
  if (n === null) return "—";
  const s = fmtNum(Math.abs(n), digits);
  return n > 0 ? `+${s}` : n < 0 ? `−${s}` : s;
}

export function fmtMoney(v: string | number | null | undefined, ccy: string, digits = 2): string {
  const s = fmtNum(v, digits);
  return s === "—" ? s : `${ccy === "INR" ? "₹" : ""}${s}${ccy === "INR" ? "" : ` ${ccy}`}`;
}

export function pnlClass(v: string | number | null | undefined): string {
  const n = num(v);
  if (n === null || n === 0) return "text-muted-foreground";
  return n > 0 ? "text-profit" : "text-loss";
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function bookId(market: string, segment: string, style: string): string {
  return `${market}-${segment}-${style}`;
}

export function shortSymbol(symbol: string): string {
  // "BTC/USDT:USDT" -> "BTC/USDT perp"
  const [pair, settle] = symbol.split(":");
  return settle && !settle.includes("-") ? `${pair} perp` : symbol;
}

/** Seconds since an ISO timestamp, measured against the client clock. */
export function ageSeconds(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null;
  return (now - new Date(iso).getTime()) / 1000;
}
