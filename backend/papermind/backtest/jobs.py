"""Background jobs (history downloads, backtests, walk-forwards, replays).

Jobs run one at a time in a worker thread with their own event loop, so the live engine keeps
running. Status/progress/results are persisted in `backtest_runs` (survive restarts; jobs that
were running during a restart are marked as interrupted).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from papermind.backtest.replay import ReplayCancelled
from papermind.backtest.runner import BacktestError
from papermind.core.ids import new_id
from papermind.core.serde import jdict
from papermind.db.base import Database
from papermind.db.models import BacktestRunRow

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]
CancelledFn = Callable[[], bool]
JobFn = Callable[[ProgressFn, CancelledFn], Coroutine[Any, Any, dict[str, Any]]]


def _now() -> datetime:
    return datetime.now(UTC)


class JobManager:
    def __init__(self, db: Database, max_workers: int = 1) -> None:
        self.db = db
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="papermind-job")
        self._cancel: dict[str, threading.Event] = {}
        self._live: dict[str, tuple[float, str]] = {}
        self._last_write: dict[str, float] = {}
        with self.db.session() as s:
            for row in s.scalars(select(BacktestRunRow).where(BacktestRunRow.status.in_(["queued", "running"]))):
                row.status, row.error, row.finished_at = "error", "interrupted by a restart", _now()

    def submit(self, kind: str, spec: dict[str, Any], fn: JobFn) -> str:
        run_id = new_id("run")
        with self.db.session() as s:
            s.add(
                BacktestRunRow(id=run_id, kind=kind, status="queued", progress=0.0, spec=jdict(spec), created_at=_now())
            )
        self._cancel[run_id] = threading.Event()
        self._pool.submit(self._run, run_id, fn)
        return run_id

    def _update(self, run_id: str, **fields: Any) -> None:
        with self.db.session() as s:
            row = s.get(BacktestRunRow, run_id)
            if row is not None:
                for k, v in fields.items():
                    setattr(row, k, v)

    def _run(self, run_id: str, fn: JobFn) -> None:
        self._update(run_id, status="running")
        ev = self._cancel[run_id]

        def progress(frac: float, msg: str) -> None:
            self._live[run_id] = (frac, msg)
            now = time.monotonic()
            if now - self._last_write.get(run_id, 0.0) > 1.0:
                self._last_write[run_id] = now
                self._update(run_id, progress=round(frac, 4))

        try:
            result: dict[str, Any] = asyncio.run(fn(progress, ev.is_set))
            self._update(run_id, status="done", progress=1.0, result=jdict(result), finished_at=_now())
        except ReplayCancelled:
            self._update(run_id, status="cancelled", finished_at=_now())
        except (BacktestError, ValueError, KeyError) as exc:
            self._update(run_id, status="error", error=str(exc), finished_at=_now())
        except Exception as exc:  # fail safe: a crashing job never takes the app down
            log.exception("job failed", extra={"run_id": run_id})
            self._update(run_id, status="error", error=f"{type(exc).__name__}: {exc}", finished_at=_now())
        finally:
            self._live.pop(run_id, None)
            self._cancel.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        ev = self._cancel.get(run_id)
        if ev is None:
            return False
        ev.set()
        return True

    def _row(self, row: BacktestRunRow, full: bool) -> dict[str, Any]:
        live = self._live.get(row.id)
        out: dict[str, Any] = {
            "id": row.id,
            "kind": row.kind,
            "status": row.status,
            "progress": live[0] if live else row.progress,
            "message": live[1] if live else None,
            "spec": row.spec,
            "error": row.error,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
        }
        res = row.result or {}
        if full:
            out["result"] = row.result
        else:
            out["summary"] = res.get("summary")
            out["verdict"] = res.get("verdict")
        return jdict(out)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(BacktestRunRow, run_id)
            return self._row(row, full=True) if row else None

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(select(BacktestRunRow).order_by(BacktestRunRow.created_at.desc()).limit(limit)).all()
            return [self._row(r, full=False) for r in rows]

    def wait(self, run_id: str, timeout: float = 60.0) -> dict[str, Any] | None:
        """Block until a job finishes (tests / CLI)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.get(run_id)
            if row and row["status"] in ("done", "error", "cancelled"):
                return row
            time.sleep(0.05)
        return self.get(run_id)

    def shutdown(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
