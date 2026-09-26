"""Pure session/calendar helpers (market hours, trading day boundaries)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from papermind.core.config import SessionConfig


def local(ts: datetime, tz: str) -> datetime:
    return ts.astimezone(ZoneInfo(tz))


def is_trading_day(cfg: SessionConfig, d: date) -> bool:
    return d.weekday() in cfg.weekdays and d not in cfg.holidays


def in_entry_window(cfg: SessionConfig, now: datetime) -> tuple[bool, str]:
    lt = local(now, cfg.timezone)
    if not is_trading_day(cfg, lt.date()):
        return False, f"{lt.date()} is not a trading day"
    t = lt.time()
    if t < cfg.entry_start:
        return False, f"entries open at {cfg.entry_start:%H:%M} {cfg.timezone} (now {t:%H:%M})"
    if t >= cfg.entry_end:
        return False, f"no new entries after {cfg.entry_end:%H:%M} {cfg.timezone} (now {t:%H:%M})"
    return True, "within entry window"


def past_square_off(cfg: SessionConfig, now: datetime) -> bool:
    if cfg.square_off is None:
        return False
    lt = local(now, cfg.timezone)
    return is_trading_day(cfg, lt.date()) and lt.time() >= cfg.square_off


def day_start(now: datetime, tz: str) -> datetime:
    """UTC instant of local midnight for the trading day containing `now`."""
    lt = local(now, tz)
    return datetime.combine(lt.date(), time(0), ZoneInfo(tz)).astimezone(UTC)


def next_day_start(now: datetime, tz: str) -> datetime:
    lt = local(now, tz)
    return datetime.combine(lt.date() + timedelta(days=1), time(0), ZoneInfo(tz)).astimezone(UTC)
