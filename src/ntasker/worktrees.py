"""Git worktrees as tracked entities -- isolated checkouts tasks run in.

A ``worktrees`` row is one ``git worktree`` of a project on a branch of its
own. Tasks point at it via ``tasks.worktree_id``; several tasks may share one
tree (follow-up work continues on the same branch). A task in a tree runs in
the tree's checkout, not in the project directory, so the queue lets it run
next to the project's ordinary runs -- the tree is a lane of its own (see
:mod:`ntasker.taskqueue`).

* **Location:** ``<repo>/.claude/worktrees/ntasker-<id>`` on branch
  ``ntasker/<id>`` -- both derived from the row id, never stored. Inside the
  repo the agent inherits the repo's trust; under ``.claude`` project
  discovery skips it. ``/.claude/worktrees/`` goes into the repo's local
  ``info/exclude`` -- no tracked file is touched.
* **The end decision:** every tree ends with the user's decision -- *merge*
  (into the main checkout's branch, then remove checkout and branch), *keep*
  (park it for rework or a follow-up), or *discard* (remove checkout and
  branch; deferred by :data:`DISCARD_DELAY` so it can be undone). A tree is
  *pending* while it is ``active`` and has no open task attached -- derived,
  never written, so however a task gets closed, the tree cannot be orphaned.
* **Merge:** :func:`merge` merges itself when that is clean; otherwise it
  raises :class:`MergeNotClean` and the caller hands the job to an agent run
  (state ``merging``). :func:`sweep`, called by the queue worker, finalises
  merges and due discards.

States: ``active`` | ``kept`` | ``merging`` | ``discarding`` | ``merged`` |
``discarded`` (the last two are final; the rows stay as history).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime, timedelta

from ntasker.i18n import _

GIT_TIMEOUT = 30

# Seconds between the discard click and the actual removal -- the Undo window.
DISCARD_DELAY = 20

# States a task may join / a decision may be taken in.
OPEN_STATES = ("active", "kept")
# States the default listing shows.
LISTED_STATES = ("active", "kept", "merging", "discarding")


class WorktreeError(RuntimeError):
    """A worktree operation is not possible; the message says why."""


class MergeNotClean(WorktreeError):
    """The merge cannot run by itself -- an agent has to do it. Message = the reason."""


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WorktreeError(str(e)) from e


def _checked(cwd: str, *args: str) -> str:
    out = _git(cwd, *args)
    if out.returncode != 0:
        raise WorktreeError((out.stderr or out.stdout).strip() or f"git {args[0]} failed")
    return out.stdout.strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def repo_root(directory: str | None) -> str | None:
    """Top level of the git repo ``directory`` lies in, or ``None``."""
    if not directory or not os.path.isdir(directory):
        return None
    try:
        out = _git(directory, "rev-parse", "--show-toplevel")
    except WorktreeError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def branch_name(wt_id: int) -> str:
    """The tree's branch: ``ntasker/<id>``."""
    return f"ntasker/{wt_id}"


def path_for(project_dir: str, wt_id: int) -> str:
    """Where tree ``wt_id``'s checkout lives inside ``project_dir``."""
    return os.path.join(os.path.realpath(project_dir), ".claude", "worktrees", f"ntasker-{wt_id}")


def _project_dir(project: str) -> str | None:
    from ntasker.claude_runner import default_cwd_for_project  # noqa: PLC0415 -- lazy: avoid cycle

    return default_cwd_for_project(project)


def _root(row: sqlite3.Row) -> str:
    root = repo_root(_project_dir(row["project"]))
    if root is None:
        raise WorktreeError(_("{project} is not a git repository").format(project=row["project"]))
    return root


def _exclude(root: str) -> None:
    """List ``.claude/worktrees/`` in the repo's local ``info/exclude`` (once)."""
    common = _checked(root, "rev-parse", "--git-common-dir")
    path = os.path.join(root, common, "info", "exclude")
    line = "/.claude/worktrees/"
    try:
        with open(path, encoding="utf-8") as fh:
            if line in fh.read().splitlines():
                return
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{line}\n")


def get(conn: sqlite3.Connection, wt_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM worktrees WHERE id = ?", (wt_id,)).fetchone()


def create(conn: sqlite3.Connection, project: str | None) -> sqlite3.Row:
    """A new tree for ``project``: the row plus its checkout on a fresh branch.

    Raises :class:`WorktreeError` when the project directory is not a git repo
    or git fails -- then no row is left behind.
    """
    root = repo_root(_project_dir(project)) if project else None
    if root is None:
        raise WorktreeError(_("Worktree runs need a project whose directory is a git repository"))
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise WorktreeError(_("The repository has no commit yet"))
    branch = _git(root, "symbolic-ref", "--short", "HEAD")
    wt_id = conn.execute(
        "INSERT INTO worktrees (project, base_branch, base_sha) VALUES (?, ?, ?)",
        (project, branch.stdout.strip() or None, head.stdout.strip()),
    ).lastrowid
    try:
        _exclude(root)
        _git(root, "worktree", "prune")
        _checked(root, "worktree", "add", "-b", branch_name(wt_id), path_for(root, wt_id))
    except WorktreeError:
        conn.execute("DELETE FROM worktrees WHERE id = ?", (wt_id,))
        raise
    return get(conn, wt_id)


def ensure(row: sqlite3.Row) -> str:
    """The tree's checkout directory -- re-added from its branch if it was deleted by hand.

    The spawn path's safety net. Raises :class:`WorktreeError` for a tree that
    is no longer open (merged, discarded, being merged or discarded).
    """
    if row["state"] not in OPEN_STATES:
        raise _state_error(row)
    root = _root(row)
    path = path_for(root, row["id"])
    if not os.path.isdir(path):
        _git(root, "worktree", "prune")
        _checked(root, "worktree", "add", path, branch_name(row["id"]))
    return path


def open_task_ids(conn: sqlite3.Connection, wt_id: int) -> list[int]:
    """Attached tasks that are still open (not done, not archived)."""
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM tasks WHERE worktree_id = ? AND status = 'open' AND archived = 0",
            (wt_id,),
        )
    ]


def live_task(conn: sqlite3.Connection, wt_id: int, live: set[int]) -> int | None:
    """An attached task with a live session, or ``None``."""
    for r in conn.execute("SELECT id FROM tasks WHERE worktree_id = ?", (wt_id,)):
        if r["id"] in live:
            return r["id"]
    return None


def summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """JSON view of a tree, without git calls (cheap enough for every poll)."""
    tasks = conn.execute(
        "SELECT id, title, status, archived FROM tasks WHERE worktree_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()
    open_ = any(t["status"] == "open" and not t["archived"] for t in tasks)
    return {
        "id": row["id"],
        "project": row["project"],
        "branch": branch_name(row["id"]),
        "state": row["state"],
        "pending": row["state"] == "active" and not open_,
        "base_branch": row["base_branch"],
        "base_sha": row["base_sha"],
        "tip_sha": row["tip_sha"],
        "merge_task_id": row["merge_task_id"],
        "discard_at": row["discard_at"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in tasks],
    }


def facts(row: sqlite3.Row) -> dict:
    """What the decision dialog shows: commits on the branch, uncommitted changes, checkout present."""
    try:
        root = _root(row)
    except WorktreeError:
        return {"commits": 0, "dirty": False, "exists": False}
    path = path_for(root, row["id"])
    count = _git(root, "rev-list", "--count", f"{row['base_sha']}..{branch_name(row['id'])}")
    exists = os.path.isdir(path)
    dirty = exists and bool(_git(path, "status", "--porcelain").stdout.strip())
    return {
        "commits": int(count.stdout.strip() or 0) if count.returncode == 0 else 0,
        "dirty": dirty,
        "exists": exists,
    }


def _state_error(row: sqlite3.Row) -> WorktreeError:
    return WorktreeError(
        _("Worktree {branch} is {state}").format(branch=branch_name(row["id"]), state=row["state"])
    )


def _require(row: sqlite3.Row | None, states: tuple[str, ...]) -> sqlite3.Row:
    if row is None:
        raise WorktreeError(_("Worktree not found"))
    if row["state"] not in states:
        raise _state_error(row)
    return row


def check_join(conn: sqlite3.Connection, project: str | None, wt_id: int, *, join: bool) -> None:
    """Raise :class:`WorktreeError` unless a task of ``project`` may be in tree ``wt_id``.

    The tree must be open and belong to the same project. ``join``: the task
    joins it now -- a ``kept`` tree is in use again (``active``), so its
    decision comes up again once its tasks are done.
    """
    tree = get(conn, wt_id)
    if tree is None or tree["state"] not in OPEN_STATES:
        raise WorktreeError(_("This worktree is no longer open"))
    if tree["project"] != project:
        raise WorktreeError(_("The worktree belongs to another project -- leave it first"))
    if join and tree["state"] == "kept":
        conn.execute("UPDATE worktrees SET state = 'active' WHERE id = ?", (wt_id,))


def keep(conn: sqlite3.Connection, wt_id: int) -> None:
    """Park the tree for rework or a follow-up: checkout and branch stay."""
    _require(get(conn, wt_id), ("active",))
    conn.execute("UPDATE worktrees SET state = 'kept' WHERE id = ?", (wt_id,))


def discard(conn: sqlite3.Connection, wt_id: int, live: set[int]) -> str:
    """Schedule the discard -- the removal runs after :data:`DISCARD_DELAY` seconds.

    Until then :func:`undo_discard` takes it back. Refused while an attached
    task's session runs. Returns the moment the discard runs.
    """
    _require(get(conn, wt_id), OPEN_STATES)
    holder = live_task(conn, wt_id, live)
    if holder is not None:
        raise WorktreeError(_("Task #{id} is still running in this worktree").format(id=holder))
    at = (datetime.now() + timedelta(seconds=DISCARD_DELAY)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE worktrees SET state = 'discarding', discard_at = ? WHERE id = ?", (at, wt_id)
    )
    return at


def undo_discard(conn: sqlite3.Connection, wt_id: int) -> None:
    """Take a scheduled discard back: the tree is undecided (``active``) again."""
    _require(get(conn, wt_id), ("discarding",))
    conn.execute(
        "UPDATE worktrees SET state = 'active', discard_at = NULL WHERE id = ?", (wt_id,)
    )


def _remove(root: str, wt_id: int, *, force_branch: bool) -> str | None:
    """Remove checkout and branch; returns the branch tip (``None`` when the branch is gone)."""
    branch = branch_name(wt_id)
    tip = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").stdout.strip() or None
    path = path_for(root, wt_id)
    if os.path.isdir(path):
        _checked(root, "worktree", "remove", "--force", path)
    _git(root, "worktree", "prune")
    if tip:
        _checked(root, "branch", "-D" if force_branch else "-d", branch)
    return tip


def _finish_discard(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    tip = _remove(_root(row), row["id"], force_branch=True)
    conn.execute(
        "UPDATE worktrees SET state = 'discarded', tip_sha = COALESCE(?, tip_sha), "
        "discard_at = NULL, decided_at = ? WHERE id = ?",
        (tip, _now(), row["id"]),
    )


def merge(conn: sqlite3.Connection, wt_id: int, live: set[int], held: dict[str, int]) -> None:
    """Merge the tree's branch into the main checkout and clean up -- when that is clean.

    ``live`` = task ids with a live session, ``held`` = directory -> holder
    (see :func:`ntasker.locks.held_dirs`). Refused (:class:`WorktreeError`)
    while a task is open or running in the tree, or with nothing to merge.
    Raises :class:`MergeNotClean` when the merge needs an agent: the main
    checkout is dirty, detached or in use, the tree has uncommitted changes,
    or the merge conflicts (then it is aborted and the main checkout is as
    before).
    """
    row = _require(get(conn, wt_id), OPEN_STATES)
    open_ = open_task_ids(conn, wt_id)
    if open_:
        raise WorktreeError(_("Task #{id} in this worktree is still open").format(id=open_[0]))
    holder = live_task(conn, wt_id, live)
    if holder is not None:
        raise WorktreeError(_("Task #{id} is still running in this worktree").format(id=holder))
    root = _root(row)
    branch = branch_name(wt_id)
    if not facts(row)["commits"]:
        raise WorktreeError(_("The worktree has no commits to merge -- discard it instead"))
    if holder := held.get(os.path.realpath(root)):
        raise MergeNotClean(_("the project directory is in use by task #{id}").format(id=holder))
    if _git(root, "symbolic-ref", "--quiet", "HEAD").returncode != 0:
        raise MergeNotClean(_("the main checkout is on a detached HEAD"))
    if _git(root, "status", "--porcelain").stdout.strip():
        raise MergeNotClean(_("the main checkout has uncommitted changes"))
    path = path_for(root, wt_id)
    if os.path.isdir(path) and _git(path, "status", "--porcelain").stdout.strip():
        raise MergeNotClean(_("the worktree has uncommitted changes"))
    if _git(root, "merge", "--no-ff", "--no-edit", branch).returncode != 0:
        _git(root, "merge", "--abort")
        raise MergeNotClean(_("the merge has conflicts"))
    _finish_merge(conn, row, root)


def _finish_merge(conn: sqlite3.Connection, row: sqlite3.Row, root: str) -> None:
    tip = _remove(root, row["id"], force_branch=False)
    conn.execute(
        "UPDATE worktrees SET state = 'merged', tip_sha = COALESCE(?, tip_sha), decided_at = ? "
        "WHERE id = ?",
        (tip, _now(), row["id"]),
    )


def start_merging(conn: sqlite3.Connection, wt_id: int, task_id: int) -> None:
    """Record the agent merge run ``task_id`` for the tree."""
    conn.execute(
        "UPDATE worktrees SET state = 'merging', merge_task_id = ? WHERE id = ?", (task_id, wt_id)
    )


def merge_job(row: sqlite3.Row, reason: str) -> str:
    """The merge task's description -- the agent's whole brief."""
    branch = branch_name(row["id"])
    target = row["base_branch"] or "the branch the main checkout should be on"
    return "\n".join([
        f"Merge the git branch `{branch}` into `{target}` in this project's main checkout "
        f"(the project directory itself, not a worktree).",
        "",
        f"nTasker could not do it by itself: {reason}.",
        "",
        "- Uncommitted changes in the main checkout or in the worktree "
        f"(`.claude/worktrees/ntasker-{row['id']}`) are someone's work: commit them where they belong "
        "or ask -- never throw them away silently.",
        "- `git merge --no-ff` the branch; on conflicts keep both sides' intent, then run the project's tests.",
        "- One merge commit, no push, do not delete the branch or the worktree -- nTasker cleans up once the "
        "branch is merged.",
    ])


def sweep(live: set[int]) -> None:
    """Finalise merges and due discards. Called by the queue worker on every tick.

    A ``merging`` tree whose merge task is done, deleted, or neither queued nor
    running any more is *merged* when its branch is an ancestor of the main
    checkout's ``HEAD`` (checkout and branch removed) and back to ``active``
    (pending again) otherwise. A ``discarding`` tree past its ``discard_at`` is
    discarded. Cheap while nothing is merging or discarding: one small query.
    """
    from ntasker.db import get_conn  # noqa: PLC0415 -- lazy: avoid cycle

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT w.*, t.id AS task_id, t.status AS task_status, t.queue_order "
            "FROM worktrees w LEFT JOIN tasks t ON t.id = w.merge_task_id "
            "WHERE w.state = 'merging' OR (w.state = 'discarding' AND w.discard_at <= ?)",
            (_now(),),
        ).fetchall()
        for row in rows:
            try:
                if row["state"] == "discarding":
                    _finish_discard(conn, row)
                    continue
                finished = (
                    row["task_id"] is None
                    or row["task_status"] == "done"
                    or (row["queue_order"] is None and row["task_id"] not in live)
                )
                if not finished:
                    continue
                root = _root(row)
                merged = _git(
                    root, "merge-base", "--is-ancestor", branch_name(row["id"]), "HEAD"
                ).returncode == 0
                if merged:
                    _finish_merge(conn, row, root)
                else:
                    conn.execute("UPDATE worktrees SET state = 'active' WHERE id = ?", (row["id"],))
            except WorktreeError:
                continue   # git trouble: try again next tick, the state is unchanged
