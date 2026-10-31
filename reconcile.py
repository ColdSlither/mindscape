"""Keep the Mindscape ledger current without a human having to poll it.

**The defect this fixes.** The ledger used to be poll-driven: `updated_at` moved
only when somebody called `mindscape_status`. So a run that had silently died was
indistinguishable from one working hard, until a person happened to ask. That is a
system demanding trust where it should be offering evidence, which is the same
disease this lane spent weeks fighting.

**What this does.** One daemon thread per long-lived process. It asks the ledger
for runs that are not in a terminal state and have not been polled recently, polls
each through the same public `hermes peer status --json` path the tools use, and
writes the outcome back. Liveness is recorded in `last_polled_at` / `poll_count`,
so a viewer can tell "still running" apart from "nobody has looked in an hour".

**Cost.** Nothing while the mesh is idle: the loop wakes, finds no eligible runs,
and sleeps the idle interval. It is a thread inside a process that is already
running, not a new daemon.

**Why staleness is judged from `last_polled_at` rather than a local timer.** The
plugin loads into every process, including short-lived CLI invocations. Judging
from the ledger means two processes cannot stampede one peer, and a process that
dies simply stops contributing.

**One source of truth for terminal states.** `tools._TERMINAL` is the only list of
terminal statuses; this module derives its query from it rather than keeping a
second copy that would drift.

Set ``MINDSCAPE_RECONCILE=0`` to disable.
"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

ACTIVE_INTERVAL_S = 10.0   # how often to re-check a run believed to be in flight
IDLE_INTERVAL_S = 60.0     # how often to re-check whether anything is in flight
_MAX_BATCH = 8

_started = False
_stop = threading.Event()
_lock = threading.Lock()


def _terminal_states() -> tuple:
    """Terminal statuses, read from the tools module so there is one list."""
    from .tools import _TERMINAL

    return tuple(_TERMINAL)


# ------------------------------------------------------------------ ledger

def _ensure_columns(con) -> None:
    """Add liveness columns to an existing ledger without disturbing rows."""
    have = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    for column, decl in (("last_polled_at", "REAL"), ("poll_count", "INTEGER DEFAULT 0")):
        if column not in have:
            con.execute(f"ALTER TABLE runs ADD COLUMN {column} {decl}")
    con.commit()


def _eligible(con, terminal: tuple, stale_after: float, limit: int = _MAX_BATCH):
    """Runs that are not in a terminal state and have not been polled recently.

    Written as NOT IN <terminal> rather than IN <in-flight> on purpose: statuses we
    have never seen still count as in-flight, so an unfamiliar state gets watched
    instead of silently ignored.
    """
    placeholders = ",".join("?" * len(terminal))
    rows = con.execute(
        f"SELECT run_id, node, agent FROM runs "
        f"WHERE (status IS NULL OR status NOT IN ({placeholders})) "
        f"  AND (last_polled_at IS NULL OR last_polled_at < ?) "
        f"ORDER BY COALESCE(last_polled_at, 0) ASC LIMIT ?",
        (*terminal, time.time() - stale_after, limit),
    ).fetchall()
    return rows


def _mark_polled(run_id: str) -> None:
    from .tools import _db

    con = _db()
    con.execute(
        "UPDATE runs SET last_polled_at=?, poll_count=COALESCE(poll_count,0)+1 WHERE run_id=?",
        (time.time(), run_id),
    )
    con.commit()


# ------------------------------------------------------------------ polling

def _poll_run(row, terminal: tuple) -> None:
    from .tools import _poll, _update

    run_id, node, agent = row[0], row[1], (row[2] or "")
    polled, _proc = _poll(node, agent, run_id)
    if not polled:
        # A transport problem, not a run state. Record the attempt so one dead peer
        # cannot be retried in a tight loop, and let staleness speak for itself.
        _mark_polled(run_id)
        return
    status = str(polled.get("status") or "unknown")
    _update(run_id, status, str(polled.get("output") or ""), str(polled.get("error") or ""))
    _mark_polled(run_id)
    if status.strip().lower() in terminal:
        logger.info("Mindscape reconcile: %s on %s reached %s", run_id, node, status)


def _loop() -> None:
    from .tools import _db

    while not _stop.is_set():
        interval = IDLE_INTERVAL_S
        try:
            terminal = _terminal_states()
            con = _db()
            _ensure_columns(con)
            rows = _eligible(con, terminal, stale_after=ACTIVE_INTERVAL_S * 0.8)
            if rows:
                interval = ACTIVE_INTERVAL_S
                for row in rows:
                    if _stop.is_set():
                        break
                    try:
                        _poll_run(row, terminal)
                    except Exception:
                        logger.debug("Mindscape reconcile: poll failed", exc_info=True)
                        try:
                            _mark_polled(row[0])
                        except Exception:
                            pass
        except Exception:
            logger.debug("Mindscape reconcile: loop iteration failed", exc_info=True)
        _stop.wait(interval)


# ------------------------------------------------------------------- control

def start_poller() -> bool:
    """Start the reconciler once per process. True if this call started it."""
    if os.environ.get("MINDSCAPE_RECONCILE", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    global _started
    with _lock:
        if _started:
            return False
        _started = True
        _stop.clear()
        threading.Thread(target=_loop, name="mindscape-reconcile", daemon=True).start()
        return True


def stop_poller() -> None:
    _stop.set()
