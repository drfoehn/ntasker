"""Waiting for a directory lock -- the locks' own queue.

A running task that needs a directory another live task holds asks with
``ntasker lock add <id> <project> --wait``. Instead of a refusal the request is
stored (``lock_requests``), the holder is told who waits, and the queue worker
grants the lock on the first tick all requested directories are free
(:func:`grant`), oldest request first. The waiter gets a message and continues.

A wait that would close a circle -- the holder already waits, directly or
through others, for the requester (lock requests and dependencies alike) -- is
refused as a deadlock (:func:`deadlock`). Both sides are told; the agents agree
who gives way (see ``docs/messaging.md``).
"""

from __future__ import annotations

import sqlite3

from ntasker import locks, messages


def pending(conn: sqlite3.Connection, task_id: int) -> list[str]:
    """The projects ``task_id`` waits to lock, oldest first."""
    rows = conn.execute(
        "SELECT project FROM lock_requests WHERE task_id = ? ORDER BY created_at, rowid", (task_id,)
    ).fetchall()
    return [r["project"] for r in rows]


def wants(conn: sqlite3.Connection, task_id: int, projects: list[str]) -> bool:
    """Whether ``task_id`` waits to lock any of ``projects``."""
    return bool(set(pending(conn, task_id)) & set(projects))


def _waits_on(conn: sqlite3.Connection, task_id: int, held: dict[str, int]) -> set[int]:
    """Tasks ``task_id`` waits for: holders of the directories it requested, open dependencies."""
    out = {held[d] for d in (locks.resolve_dir(p) for p in pending(conn, task_id)) if d in held}
    out |= {
        int(r["depends_on_id"])
        for r in conn.execute(
            "SELECT d.depends_on_id FROM task_deps d JOIN tasks t ON t.id = d.depends_on_id "
            "WHERE d.task_id = ? AND t.status != 'done'",
            (task_id,),
        )
    }
    out.discard(task_id)
    return out


def deadlock(
    conn: sqlite3.Connection, requester: int, target: int, held: dict[str, int]
) -> list[int] | None:
    """The chain ``target -> ... -> requester`` if ``target`` already waits for ``requester``.

    ``None`` when ``requester`` may wait for ``target`` without closing a circle.
    """
    paths = {target: [target]}
    todo = [target]
    while todo:
        cur = todo.pop()
        for nxt in _waits_on(conn, cur, held):
            if nxt == requester:
                return [*paths[cur], requester]
            if nxt not in paths:
                paths[nxt] = [*paths[cur], nxt]
                todo.append(nxt)
    return None


def deadlock_text(chain: list[int]) -> str:
    """How a refused wait reads: the circle and what to do about it."""
    other = chain[0]
    circle = " -> ".join(f"#{t}" for t in [chain[-1], *chain])
    return (
        f"deadlock: #{other} already waits for #{chain[-1]} ({circle}). Agree with #{other} "
        "who gives way: the one less far along releases its lock (`ntasker lock rm`) or "
        "finishes first; the other waits."
    )


def tell_deadlock(conn: sqlite3.Connection, requester: int, chain: list[int], need: str) -> None:
    """Start the talk: tell the other side of a refused wait about the circle."""
    other = chain[0]
    circle = " -> ".join(f"#{t}" for t in [requester, *chain])
    messages.post(
        conn,
        other,
        f"Task #{requester} needs {need}, but you already wait for it ({circle}) -- a deadlock. "
        f"Agree with #{requester} which of you gives way: the one less far along releases its "
        "lock (`ntasker lock rm`) or finishes first; the other waits.",
        kind="wait",
        from_task=requester,
    )
    conn.commit()  # the caller refuses with an exception, which would roll this back


def request(
    conn: sqlite3.Connection, task_id: int, projects: list[str], live: set[int]
) -> list[dict]:
    """Queue ``task_id`` for ``projects``; returns ``[{project, holder}]`` of the held ones.

    Called when at least one of them is held by another live task. All
    requested projects are granted together later (all or nothing, like
    ``lock add``). Raises :class:`ntasker.messages.MessageError` (409) on a
    deadlock -- nothing is stored then, the other side is told.
    """
    held = locks.held_dirs(conn, live)
    blocked = []
    for name in projects:
        holder = held.get(locks.resolve_dir(name))
        if holder is not None and holder != task_id:
            blocked.append({"project": name, "holder": holder})
    for b in blocked:
        chain = deadlock(conn, task_id, b["holder"], held)
        if chain is not None:
            tell_deadlock(conn, task_id, chain, f"the lock on {b['project']}")
            raise messages.MessageError(409, deadlock_text(chain))
    conn.executemany(
        "INSERT OR IGNORE INTO lock_requests (task_id, project) VALUES (?, ?)",
        [(task_id, p) for p in projects],
    )
    title = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()["title"]
    for holder in sorted({b["holder"] for b in blocked}):
        names = ", ".join(b["project"] for b in blocked if b["holder"] == holder)
        row = conn.execute("SELECT project FROM tasks WHERE id = ?", (holder,)).fetchone()
        own = row["project"] if row is not None else None
        text = (
            f"Task #{task_id} {title} waits for your lock on {names}; "
            "it gets it automatically once free."
        )
        if own in names.split(", "):
            text += f"\n{own} is your own project: it stays held until #{holder} is done."
        text += (
            f"\nNothing to answer: once your work is done and you may commit, commit and run "
            f"`ntasker finish {holder}`; a lock you no longer need: "
            f"`ntasker lock rm {holder} <project>`. If you must wait for the user first, "
            f"tell #{task_id} once."
        )
        messages.post(conn, holder, text, kind="wait", from_task=task_id)
    return blocked


def grant(conn: sqlite3.Connection, live: set[int]) -> list[int]:
    """Hand freed directories to their waiters; returns the tasks granted.

    Oldest request first. A request of a task that no longer runs (or is
    done) is dropped -- a queued start re-checks its directories anyway.
    """
    rows = conn.execute(
        "SELECT r.task_id, t.project, t.locks, t.status, MIN(r.created_at) AS since "
        "FROM lock_requests r JOIN tasks t ON t.id = r.task_id "
        "GROUP BY r.task_id ORDER BY since, MIN(r.rowid)"
    ).fetchall()
    if not rows:
        return []
    held = locks.held_dirs(conn, live)
    granted = []
    for row in rows:
        tid = int(row["task_id"])
        if tid not in live or row["status"] == "done":
            conn.execute("DELETE FROM lock_requests WHERE task_id = ?", (tid,))
            continue
        wanted = pending(conn, tid)
        dirs = {locks.resolve_dir(p) for p in wanted}
        if locks.conflict(dirs, held, exclude=tid) is not None:
            continue
        new = locks.normalize([*locks.parse(row["locks"]), *wanted], row["project"])
        conn.execute("UPDATE tasks SET locks = ? WHERE id = ?", (locks.dump(new), tid))
        conn.execute("DELETE FROM lock_requests WHERE task_id = ?", (tid,))
        for d in dirs:
            held.setdefault(d, tid)
        messages.post(
            conn,
            tid,
            f"nTasker granted you the lock on {', '.join(wanted)} -- it is free now. "
            "Continue your work.",
            kind="lock",
        )
        granted.append(tid)
    return granted
