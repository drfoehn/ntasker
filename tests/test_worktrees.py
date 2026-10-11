"""Git worktrees: tracked trees, shared by tasks, ending in the user's decision."""

from __future__ import annotations

import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, cli, locks, taskqueue, worktrees
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path

BASE = "http://127.0.0.1:8766"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _commit(cwd, name, text="x"):
    (cwd / name).write_text(text)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    set_db_path(db)
    init_db(db)
    monkeypatch.delenv("NTASKER_DIR_LOCKS", raising=False)
    # Project name -> tmp_path/<name>, everywhere a project directory is resolved.
    monkeypatch.setattr(locks, "resolve_dir", lambda name: str(tmp_path / name))
    monkeypatch.setattr(claude_runner, "default_cwd_for_project", lambda p: str(tmp_path / p) if p else None)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "README", "base\n")
    (tmp_path / "plain").mkdir()
    claude_runner.SESSIONS.clear()
    yield tmp_path
    claude_runner.SESSIONS.clear()


@pytest.fixture
def client(env):
    return TestClient(app, base_url=BASE)


def _task(client, title="a", project="repo", **kw):
    return client.post("/api/tasks", json={"title": title, "project": project, **kw}).json()


def _new_tree(client, task_id):
    r = client.post(f"/api/tasks/{task_id}/worktree")
    assert r.status_code == 201, r.text
    return r.json()


def _path(env, wt_id):
    return worktrees.path_for(str(env / "repo"), wt_id)


def _checkout(env, wt_id):
    return env / "repo" / ".claude" / "worktrees" / f"ntasker-{wt_id}"


def _done(client, task_id):
    assert client.patch(f"/api/tasks/{task_id}", json={"status": "done"}).status_code == 200


def _tree(client, wt_id):
    return client.get(f"/api/worktrees/{wt_id}").json()


# -- creating and joining ----------------------------------------------------


def test_new_tree_checkout_branch_and_exclude(client, env):
    t = _task(client)
    w = _new_tree(client, t["id"])
    assert w["branch"] == f"ntasker/{w['id']}" and w["state"] == "active" and not w["pending"]
    path = _path(env, w["id"])
    assert path.endswith(f".claude/worktrees/ntasker-{w['id']}")
    assert _git(path, "branch", "--show-current").strip() == w["branch"]
    assert _git(env / "repo", "status", "--porcelain") == ""   # excluded locally
    assert client.get(f"/api/tasks/{t['id']}").json()["worktree_id"] == w["id"]
    assert client.post(f"/api/tasks/{t['id']}/worktree").status_code == 409


def test_new_tree_needs_a_git_project(client):
    t = _task(client, project="plain")
    r = client.post(f"/api/tasks/{t['id']}/worktree")
    assert r.status_code == 400 and "git" in r.json()["detail"]
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0] == 0


def test_join_leave_and_project_rules(client, env):
    a = _task(client)
    w = _new_tree(client, a["id"])
    b = _task(client, "b", worktree_id=w["id"])
    assert b["worktree_id"] == w["id"]
    other = _task(client, "o", project="plain")
    assert client.patch(f"/api/tasks/{other['id']}", json={"worktree_id": w["id"]}).status_code == 400
    # A task in a tree cannot change project without leaving it first.
    assert client.patch(f"/api/tasks/{b['id']}", json={"project": "plain"}).status_code == 400
    assert client.patch(f"/api/tasks/{b['id']}", json={"worktree_id": None}).json()["worktree_id"] is None
    assert client.patch(f"/api/tasks/{b['id']}", json={"project": "plain"}).status_code == 200


def test_ensure_recreates_a_hand_deleted_checkout(client, env):
    a = _task(client)
    w = _new_tree(client, a["id"])
    path = _path(env, w["id"])
    _git(env / "repo", "worktree", "remove", "--force", path)
    with get_conn() as conn:
        assert worktrees.ensure(worktrees.get(conn, w["id"])) == path
    assert os.path.isdir(path)


# -- pending + decisions -----------------------------------------------------


def test_pending_is_derived_from_open_tasks(client, env):
    a = _task(client)
    w = _new_tree(client, a["id"])
    b = _task(client, "b", worktree_id=w["id"])
    _done(client, a["id"])
    assert not _tree(client, w["id"])["pending"]
    # Closed behind the API's back, like `ntasker done` -- still caught.
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (b["id"],))
    listing = client.get("/api/worktrees").json()
    assert listing[0]["id"] == w["id"] and listing[0]["pending"]


def test_keep_then_join_makes_it_pending_again(client, env):
    a = _task(client)
    w = _new_tree(client, a["id"])
    _done(client, a["id"])
    assert client.post(f"/api/worktrees/{w['id']}/keep").json() == {"state": "kept"}
    assert not _tree(client, w["id"])["pending"]
    b = _task(client, "follow-up", worktree_id=w["id"])
    assert _tree(client, w["id"])["state"] == "active"
    _done(client, b["id"])
    assert _tree(client, w["id"])["pending"]


def test_discard_is_deferred_and_undoable(client, env):
    a = _task(client)
    w = _new_tree(client, a["id"])
    _commit(_checkout(env, w["id"]), "f.txt")
    claude_runner.SESSIONS[a["id"]] = type("S", (), {"alive": True})()  # type: ignore[assignment]
    assert client.post(f"/api/worktrees/{w['id']}/discard").status_code == 409
    claude_runner.SESSIONS.clear()
    r = client.post(f"/api/worktrees/{w['id']}/discard").json()
    assert r["state"] == "discarding" and r["delay"] == worktrees.DISCARD_DELAY
    worktrees.sweep(set())   # not due yet
    assert os.path.isdir(_path(env, w["id"]))
    assert client.post(f"/api/worktrees/{w['id']}/undo").json() == {"state": "active"}
    client.post(f"/api/worktrees/{w['id']}/discard")
    with get_conn() as conn:
        conn.execute("UPDATE worktrees SET discard_at = '2000-01-01T00:00:00' WHERE id = ?", (w["id"],))
    worktrees.sweep(set())
    t = _tree(client, w["id"])
    assert t["state"] == "discarded" and t["tip_sha"]
    assert not os.path.isdir(_path(env, w["id"]))
    assert _git(env / "repo", "branch", "--list", t["branch"]) == ""
    assert client.post(f"/api/worktrees/{w['id']}/undo").status_code == 409


def test_merge_fast_path(client, env):
    a = _task(client)
    w = _new_tree(client, a["id"])
    assert client.post(f"/api/worktrees/{w['id']}/merge").status_code == 409   # task open
    _done(client, a["id"])
    assert client.post(f"/api/worktrees/{w['id']}/merge").status_code == 409   # nothing to merge
    _commit(_checkout(env, w["id"]), "feature.txt")
    assert _tree(client, w["id"])["facts"]["commits"] == 1
    assert client.post(f"/api/worktrees/{w['id']}/merge").json() == {"state": "merged"}
    assert (env / "repo" / "feature.txt").exists()
    assert "Merge branch" in _git(env / "repo", "log", "-1", "--format=%s")
    assert not os.path.isdir(_path(env, w["id"]))
    assert _git(env / "repo", "branch", "--list", w["branch"]) == ""
    assert client.get("/api/worktrees").json() == []
    assert client.get("/api/worktrees?all=true").json()[0]["state"] == "merged"


def test_merge_conflict_queues_an_agent_run(client, env, monkeypatch):
    monkeypatch.setattr("ntasker.app.terminal_available", lambda spec: (True, None))
    a = _task(client)
    w = _new_tree(client, a["id"])
    _commit(_checkout(env, w["id"]), "README", "tree\n")
    _commit(env / "repo", "README", "main\n")
    _done(client, a["id"])
    r = client.post(f"/api/worktrees/{w['id']}/merge")
    assert r.status_code == 202
    merge_task = r.json()["task_id"]
    assert _git(env / "repo", "status", "--porcelain") == ""   # aborted cleanly
    t = client.get(f"/api/tasks/{merge_task}").json()
    assert t["fasttrack"] and t["project"] == "repo" and w["branch"] in t["description"]
    with get_conn() as conn:
        assert conn.execute("SELECT queue_order FROM tasks WHERE id = ?", (merge_task,)).fetchone()[0]
    assert _tree(client, w["id"])["state"] == "merging"
    worktrees.sweep(set())   # still queued -> not finalised
    assert _tree(client, w["id"])["state"] == "merging"
    # The agent failed and the task left the queue: undecided again.
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET queue_order = NULL WHERE id = ?", (merge_task,))
    worktrees.sweep(set())
    assert _tree(client, w["id"])["pending"]
    # Second attempt: the "agent" merges, its task is done -> finalised.
    merge_task = client.post(f"/api/worktrees/{w['id']}/merge").json()["task_id"]
    subprocess.run(["git", "-C", str(env / "repo"), "merge", "-q", w["branch"]], capture_output=True)
    (env / "repo" / "README").write_text("both\n")
    _git(env / "repo", "commit", "-q", "-am", "merge")
    _done(client, merge_task)
    worktrees.sweep(set())
    assert _tree(client, w["id"])["state"] == "merged"
    assert not os.path.isdir(_path(env, w["id"]))


def test_diff_survives_the_cleanup(client, env):
    a = _task(client)
    w = _new_tree(client, a["id"])
    path = _path(env, w["id"])
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET run_baselines = ? WHERE id = ?",
            (f'{{"{path}": "{w["base_sha"]}"}}', a["id"]),
        )
    _commit(_checkout(env, w["id"]), "feature.txt")
    _done(client, a["id"])
    assert [f["path"] for f in client.get(f"/api/tasks/{a['id']}/diff").json()["files"]] == ["feature.txt"]
    client.post(f"/api/worktrees/{w['id']}/merge")
    assert not os.path.isdir(path)
    assert [f["path"] for f in client.get(f"/api/tasks/{a['id']}/diff").json()["files"]] == ["feature.txt"]


# -- CLI ---------------------------------------------------------------------


def test_cli_worktree_flag(env):
    db = str(env / "t.db")
    assert cli.main(["--db", db, "add", "--title", "p", "--project", "plain", "--worktree", "new"]) == 2
    assert cli.main(["--db", db, "add", "--title", "a", "--project", "repo", "--worktree", "new"]) == 0
    with get_conn() as conn:
        a = conn.execute("SELECT id, worktree_id FROM tasks WHERE title = 'a'").fetchone()
        assert a["worktree_id"]
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title = 'p'").fetchone()[0] == 0
    assert cli.main(["--db", db, "add", "--title", "b", "--project", "repo"]) == 0
    with get_conn() as conn:
        b = conn.execute("SELECT id FROM tasks WHERE title = 'b'").fetchone()["id"]
    assert cli.main(["--db", db, "patch", str(b), "--worktree", str(a["worktree_id"])]) == 0
    with get_conn() as conn:
        assert conn.execute("SELECT worktree_id FROM tasks WHERE id = ?", (b,)).fetchone()[0] == a["worktree_id"]
    assert cli.main(["--db", db, "patch", str(b), "--worktree", "none"]) == 0
    assert cli.main(["--db", db, "patch", str(b), "--worktree", "bogus"]) == 2
    with get_conn() as conn:
        assert conn.execute("SELECT worktree_id FROM tasks WHERE id = ?", (b,)).fetchone()[0] is None


def test_cli_worktree_commands(client, env, monkeypatch, capsys):
    def fake_api(base, method, path, body=None, timeout=5.0):
        r = client.request(method, path, json=body)
        return r.status_code, r.json()

    monkeypatch.setattr(cli, "_api_call", fake_api)
    a = _task(client)
    w = _new_tree(client, a["id"])
    _done(client, a["id"])
    db = ["--db", str(env / "t.db")]
    assert cli.main([*db, "worktree", "list"]) == 0
    assert w["branch"] in capsys.readouterr().out
    assert cli.main([*db, "worktree", "merge", str(w["id"])]) == 1   # nothing to merge
    assert cli.main([*db, "worktree", "discard", str(w["id"])]) == 0
    assert f"worktree undo {w['id']}" in capsys.readouterr().out
    assert cli.main([*db, "worktree", "undo", str(w["id"])]) == 0
    assert cli.main([*db, "worktree", "keep", str(w["id"])]) == 0


# -- runs: lock check, seed, spawn, lanes --------------------------------------


def test_lock_check_confines_a_tree_task_to_its_checkout(client, env, monkeypatch):
    monkeypatch.setattr(locks, "known_projects", lambda conn: {"repo", "plain"})
    t = _task(client)
    w = _new_tree(client, t["id"])
    check = lambda p: client.get("/api/locks/check", params={"task": t["id"], "path": p}).json()  # noqa: E731
    assert check(os.path.join(_path(env, w["id"]), "f.py"))["allowed"] is True
    assert check(str(env / "repo" / "f.py"))["allowed"] is False


def test_seed_names_branch_and_sibling_tasks(client, env):
    a = _task(client, "first")
    w = _new_tree(client, a["id"])
    b = _task(client, "second", worktree_id=w["id"])
    seed = claude_runner.queue_seed_for_task(client.get(f"/api/tasks/{b['id']}").json())
    assert f"`{w['branch']}`" in seed and f"#{a['id']} first" in seed
    assert "ntasker/" not in claude_runner.queue_seed_for_task({"id": 99, "title": "t", "project": "repo"})


def test_spawn_runs_in_the_worktree(client, env, monkeypatch):
    a = _task(client)
    w = _new_tree(client, a["id"])
    seen = {}
    real_popen = subprocess.Popen

    class _Proc:
        pid = 0

    def fake_popen(args, **kw):
        if args[0] == "git":   # the worktree and baseline calls stay real
            return real_popen(args, **kw)
        seen["cwd"] = kw["cwd"]
        return _Proc()

    monkeypatch.setattr(claude_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(claude_runner, "_attach_reader", lambda sess: None)
    claude_runner._start_session(a["id"], seed="go")
    path = _path(env, w["id"])
    assert seen["cwd"] == path
    assert client.get(f"/api/tasks/{a['id']}").json()["session_cwd"] == path


@pytest.fixture
def queue(env, monkeypatch):
    monkeypatch.setattr(taskqueue, "_runnable_agents", lambda: {"claude"})
    live: set[int] = set()
    started: list[int] = []
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: list(live))
    monkeypatch.setattr(taskqueue, "external_session_ids", lambda: [])

    def fake_start(task_id, seed, quick=False, resume=False):
        started.append(task_id)
        live.add(task_id)
        return True

    monkeypatch.setattr(taskqueue, "start_detached_session", fake_start)
    for s in (taskqueue._running, taskqueue.QUICK, taskqueue.RESUME, taskqueue.LANELESS, taskqueue.FORCE):
        s.clear()
    taskqueue._booted = True

    def add(title, wt=None):
        with get_conn() as conn:
            if wt is not None and worktrees.get(conn, wt) is None:
                conn.execute("INSERT INTO worktrees (id, project) VALUES (?, 'repo')", (wt,))
            return conn.execute(
                "INSERT INTO tasks (title, project, worktree_id) VALUES (?, 'repo', ?)", (title, wt)
            ).lastrowid

    return {"add": add, "live": live, "started": started}


def test_a_tree_is_a_lane_of_its_own(queue):
    """A and B share tree 1, C is in tree 2, D and E run in the project dir:
    one task per tree and one in the project dir start together."""
    a = queue["add"]("A", 1)
    b = queue["add"]("B", 1)
    c = queue["add"]("C", 2)
    d = queue["add"]("D")
    e = queue["add"]("E")
    taskqueue.set_queue([a, b, c, d, e])
    taskqueue.tick()
    assert queue["started"] == [a, c, d]
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    queue["live"].discard(a)
    taskqueue.tick()
    assert queue["started"] == [a, c, d, b]
    assert e not in queue["started"]


def test_failed_checkout_flags_the_entry_ended(queue, monkeypatch):
    def boom(*_a, **_k):
        raise worktrees.WorktreeError("nope")

    monkeypatch.setattr(taskqueue, "start_detached_session", boom)
    a = queue["add"]("A", 1)
    taskqueue.set_queue([a])
    taskqueue.tick()
    assert taskqueue.load_queue()[0]["session_ended_at"] is not None
