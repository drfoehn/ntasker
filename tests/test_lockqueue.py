"""Waiting for a directory lock: queueing, granting, deadlocks, ended holders."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, lockqueue, taskqueue
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    claude_runner.SESSIONS.clear()
    claude_runner.EXTERNAL.clear()
    yield TestClient(app, base_url="http://127.0.0.1:8766")
    claude_runner.SESSIONS.clear()
    claude_runner.EXTERNAL.clear()


def _add(title, project, locks="[]", live=True):
    with get_conn() as conn:
        tid = conn.execute(
            "INSERT INTO tasks (title, project, locks) VALUES (?, ?, ?)", (title, project, locks)
        ).lastrowid
    if live:
        claude_runner.EXTERNAL[tid] = os.getpid()  # an external session, never killed
    return tid


def _msgs(to_task):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM messages WHERE to_task = ? ORDER BY id", (to_task,)).fetchall()


def _grant():
    with get_conn() as conn:
        return lockqueue.grant(conn, set(claude_runner.EXTERNAL))


def test_held_lock_without_wait_names_the_way_out(env):
    _add("Holder", "pb", '["shared"]')
    a = _add("Waiter", "pa")
    r = env.post(f"/api/tasks/{a}/locks", json={"projects": ["shared"]})
    assert r.status_code == 409 and "--wait" in r.json()["detail"]


def test_wait_queues_tells_holder_and_grants_once_free(env):
    b = _add("Holder", "pb", '["shared"]')
    a = _add("Waiter", "pa")
    r = env.post(f"/api/tasks/{a}/locks", json={"projects": ["shared"], "wait": True})
    assert r.status_code == 202 and r.json()["waiting"] == [{"project": "shared", "holder": b}]
    (told,) = _msgs(b)
    assert told["kind"] == "wait" and "waits for your lock on shared" in told["body"]
    assert _grant() == []
    env.post(f"/api/tasks/{b}/outcome", json={"status": "ok", "report": "done"})
    assert _grant() == [a]
    assert env.get(f"/api/tasks/{a}").json()["locks"] == ["shared"]
    (granted,) = _msgs(a)
    assert granted["kind"] == "lock" and "granted you the lock on shared" in granted["body"]
    assert _grant() == []


def test_own_project_is_granted_when_the_holder_is_done(env):
    b = _add("Holder", "pb")
    a = _add("Waiter", "pa")
    env.post(f"/api/tasks/{a}/locks", json={"projects": ["pb"], "wait": True})
    assert "pb is your own project" in _msgs(b)[0]["body"]
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET phase = 'review' WHERE id = ?", (b,))
    assert _grant() == []
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (b,))
    assert _grant() == [a]


def test_crossed_waits_are_refused_as_deadlock(env):
    a = _add("A", "pa", '["x"]')
    b = _add("B", "pb", '["y"]')
    assert env.post(f"/api/tasks/{b}/locks", json={"projects": ["x"], "wait": True}).status_code == 202
    r = env.post(f"/api/tasks/{a}/locks", json={"projects": ["y"], "wait": True})
    assert r.status_code == 409 and "deadlock" in r.json()["detail"] and f"#{b}" in r.json()["detail"]
    assert "a deadlock" in _msgs(b)[-1]["body"]
    with get_conn() as conn:
        assert lockqueue.pending(conn, a) == []


def test_wait_for_a_task_that_waits_for_my_lock_is_a_deadlock(env):
    a = _add("A", "pa", '["x"]')
    b = _add("B", "pb")
    env.post(f"/api/tasks/{b}/locks", json={"projects": ["x"], "wait": True})
    r = env.post(f"/api/tasks/{a}/wait", json={"on": b})
    assert r.status_code == 409 and "deadlock" in r.json()["detail"]
    assert env.get(f"/api/tasks/{a}").json()["depends"] == []


def test_request_of_an_ended_session_is_dropped(env):
    _add("Holder", "pb", '["shared"]')
    a = _add("Waiter", "pa")
    env.post(f"/api/tasks/{a}/locks", json={"projects": ["shared"], "wait": True})
    del claude_runner.EXTERNAL[a]
    _grant()
    with get_conn() as conn:
        assert lockqueue.pending(conn, a) == []


def test_queued_dependent_hears_the_grant_not_the_release(env):
    b = _add("Holder", "pb", '["shared"]')
    a = _add("Waiter", "pa")
    env.patch(f"/api/tasks/{a}", json={"depends": [b]})
    env.post(f"/api/tasks/{a}/locks", json={"projects": ["shared"], "wait": True})
    env.delete(f"/api/tasks/{b}/locks/shared")
    assert _msgs(a) == []
    _grant()
    assert [m["kind"] for m in _msgs(a)] == ["lock"]


def test_ended_holder_session_tells_its_dependents(env):
    b = _add("Holder", "thrito")
    a = _add("Waiter", "pa")
    env.patch(f"/api/tasks/{a}", json={"depends": [b]})
    taskqueue._was_live = {a, b}
    del claude_runner.EXTERNAL[b]
    taskqueue._notify_ended({a})
    (msg,) = _msgs(a)
    assert msg["kind"] == "lock" and "released its lock on thrito" in msg["body"]
    taskqueue._notify_ended({a})
    assert len(_msgs(a)) == 1
