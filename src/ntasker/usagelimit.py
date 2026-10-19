"""Runs a Claude usage limit stopped -- parked until the limit resets, then resumed.

A usage limit (5-hour or weekly window of the subscription) is no failure of
the task: the work goes on once the limit has reset. So a run it stopped is
*paused*, not ``blocked`` -- ``tasks.limit_until`` holds the moment nTasker
resumes it. A column, not memory: the pause survives a server restart.

**Detection** -- every way a limit can stop a run ends in :func:`park`:

* the ``StopFailure`` hook (``rate_limit``) of a spawned session -- the turn
  was cut off (``POST /api/claude/sessions/{id}/limit``);
* the ``Stop`` hook of a turn that ended under a limit note in the transcript
  (:func:`ntasker.transcript.limit_stop`) -- Claude Code gave the agent a
  short grace allowance and the agent wrapped up;
* ``ntasker finish --status failed|blocked`` while the transcript shows such
  a limit -- the agent reported the limit as a blocker itself. Works for an
  external ``/task`` session too.

**Reset time** (:func:`reset_time`): the usage windows' ``resets_at``, else the
"resets 3pm" of the limit message; when neither is known, nTasker looks again
after :data:`POLL_SECONDS`. Before a due run is resumed, the windows are asked
once more (:func:`_still_limited`): a window still at 100 % postpones it.

**Resume**: :func:`ntasker.taskqueue.resume_paused` -- a live session gets
:data:`RESUME_PROMPT` typed in; an ended one is reopened with ``claude
--resume`` and the same prompt, through the queue (lane and lock rules apply).

While any run is paused, the queue starts no further Claude run
(:func:`hold_until`) -- the limit is the account's, not one session's.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta

from ntasker import claude_runner
from ntasker.agents import AGENTS, resolve_home
from ntasker.db import get_conn, insert_outcome
from ntasker.transcript import limit_stop

#: Typed into a paused session -- or sent with ``claude --resume`` -- once the limit has reset.
RESUME_PROMPT = "[nTasker] The usage limit has reset. Continue your task where you left off."

#: Seconds past the reported reset before the run is resumed -- a resume a
#: little early would just hit the limit again.
GRACE_SECONDS = 60.0

#: Summary of the ``limit`` run-log row -- stored data, not UI text.
LIMIT_SUMMARY = "usage limit reached -- paused until {until}"

#: Seconds until nTasker looks again when no reset time is known.
POLL_SECONDS = 15 * 60.0

# "resets 3pm", "resets 3:30pm", "resets 15:00" in the limit message.
_RESETS_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", re.IGNORECASE)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _epoch(stamp: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return None


def reset_from_text(text: str, now: float | None = None) -> float | None:
    """The next moment named by "resets 3pm" in a limit message (epoch seconds), or ``None``."""
    m = _RESETS_RE.search(text or "")
    if m is None:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm:
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    base = datetime.fromtimestamp(now if now is not None else time.time())
    at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= base:
        at += timedelta(days=1)
    return at.timestamp()


def detect(session_id: str | None) -> dict | None:
    """The limit that stopped this Claude session (``{"text", "at"}``), else ``None``."""
    return limit_stop(resolve_home(AGENTS["claude"]), session_id)


def reset_time(text: str = "") -> float | None:
    """When the limit resets (epoch seconds): the usage windows, else the message's "resets ...".

    ``None`` when neither says -- :func:`park` then polls.
    """
    from ntasker.plugins.claude import usage  # noqa: PLC0415 -- lazy: a plugin of its own

    return usage.limit_reset() or reset_from_text(text)


def park(task_id: int, reset: float | None, reason: str = "") -> float:
    """Pause the task's run until ``reset`` (+ :data:`GRACE_SECONDS`); returns that moment.

    ``reset`` ``None`` = unknown: look again in :data:`POLL_SECONDS`. A live
    session waits meanwhile, but not with its turn over -- pending messages
    must not go in (they would hit the limit too).

    A fasttrack run that was not paused yet gets its run-log row with status
    ``limit`` -- nTasker's own record of why the run stopped, since the agent
    is cut off and cannot report it. ``reason``: the limit message, if any.
    Moving an existing pause (a later reset) adds no row.
    """
    now = time.time()
    until = max(reset + GRACE_SECONDS, now + GRACE_SECONDS) if reset is not None else now + POLL_SECONDS
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.execute("UPDATE tasks SET limit_until = ? WHERE id = ?", (_iso(until), task_id))
        if row is not None and row["fasttrack"] and not row["limit_until"]:
            summary = LIMIT_SUMMARY.format(until=datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M"))
            if reason.strip():
                summary += f" ({reason.strip()})"
            insert_outcome(conn, row, "limit", summary[:200], report=row["report"])
    sess = claude_runner.SESSIONS.get(task_id)
    if sess is not None and sess.alive:
        sess.hook_waiting = True
        sess.turn_over = False
    return until


def clear(task_id: int) -> None:
    """End the task's pause (resumed, finished, or the user carried on)."""
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET limit_until = NULL WHERE id = ? AND limit_until IS NOT NULL", (task_id,))


def paused_until(task_id: int) -> float | None:
    """When the task's paused run is resumed (epoch seconds), ``None`` = not paused."""
    with get_conn() as conn:
        row = conn.execute("SELECT limit_until FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _epoch(row["limit_until"]) if row and row["limit_until"] else None


def _paused(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Open, paused tasks -- queue order first, then the rest."""
    return conn.execute(
        "SELECT * FROM tasks WHERE limit_until IS NOT NULL AND status = 'open' AND archived = 0 "
        "ORDER BY queue_order IS NULL, queue_order, id"
    ).fetchall()


def hold_until() -> float | None:
    """Until when new Claude runs wait (epoch seconds): the latest pause still ahead, else ``None``."""
    now = time.time()
    with get_conn() as conn:
        ahead = [t for r in _paused(conn) if (t := _epoch(r["limit_until"])) and t > now]
    return max(ahead) if ahead else None


def due() -> list[sqlite3.Row]:
    """Paused tasks whose moment has come, in queue order."""
    now = time.time()
    with get_conn() as conn:
        return [r for r in _paused(conn) if (_epoch(r["limit_until"]) or 0.0) <= now]


def still_limited() -> float | None:
    """The reset of a usage window still at 100 % (epoch seconds, latest), else ``None``.

    Asked before a due run is resumed: a guessed or polled moment can come
    before the real reset. ``None`` also when the windows cannot be read --
    then the resume is tried, and a limit that still bites parks it again.
    """
    from ntasker.plugins.claude import usage  # noqa: PLC0415 -- lazy: a plugin of its own

    now = time.time()
    windows = [w for w in (usage.snapshot(force=True) or {}).values() if w and w["utilization"] >= 100]
    later = [t for w in windows if (t := _epoch(w.get("resets_at"))) and t > now]
    return max(later) if later else None
