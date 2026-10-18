"""Usage limits: the ``StopFailure`` hook parks a session, the worker resumes it after the reset."""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, cli, plugins, taskqueue
from ntasker.app import app
from ntasker.claude_assets import hooks_settings_path
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.plugins.claude import usage


class _Proc:
    pid = os.getpid()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(plugins.ENV_DISABLED, raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    claude_runner.SESSIONS.clear()
    monkeypatch.setattr(claude_runner, "LIMIT_UNTIL", None)
    yield TestClient(app, base_url="http://127.0.0.1:8766")
    claude_runner.SESSIONS.clear()


def _spawned():
    with get_conn() as conn:
        tid = conn.execute("INSERT INTO tasks (title, project) VALUES ('T', 'p')").lastrowid
    sess = claude_runner.TermSession(task_id=tid, proc=_Proc(), master_fd=-1)  # type: ignore[arg-type]
    claude_runner.SESSIONS[tid] = sess
    return tid, sess


def _windows(five: float, week: float) -> dict:
    return {
        "five_hour": {"utilization": five, "resets_at": "2026-10-18T17:45:00+00:00"},
        "seven_day": {"utilization": week, "resets_at": "2026-10-20T08:00:00+00:00"},
    }


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


@pytest.mark.parametrize(
    ("five", "week", "expected"),
    [
        (100.0, 40.0, "2026-10-18T17:45:00+00:00"),  # the 5-hour window bites
        (100.0, 100.0, "2026-10-20T08:00:00+00:00"),  # both full: the later reset frees the session
        (40.0, 99.0, "2026-10-20T08:00:00+00:00"),  # none at 100 % yet: the fullest one
    ],
)
def test_limit_reset_picks_the_binding_window(monkeypatch, five, week, expected):
    monkeypatch.setattr(usage, "snapshot", lambda force=False: _windows(five, week))
    assert usage.limit_reset() == _epoch(expected)


def test_limit_reset_unknown_without_subscription(monkeypatch):
    monkeypatch.setattr(usage, "snapshot", lambda force=False: None)
    assert usage.limit_reset() is None


def test_stopfailure_hook_is_wired():
    for locks in (False, True):
        with open(hooks_settings_path(locks)) as f:
            entry = json.load(f)["hooks"]["StopFailure"][0]
        assert entry["matcher"] == "rate_limit"
        assert entry["hooks"][0]["command"] == "ntasker hook limit"


def test_hook_limit_reports_to_the_server(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_api_call", lambda base, method, path, body=None, timeout=5.0: calls.append(path) or (200, {}))
    monkeypatch.setenv("NTASKER_TASK_ID", "7")
    monkeypatch.setenv("NTASKER_URL", "http://127.0.0.1:8766")
    payload = {"hook_event_name": "StopFailure", "error": "rate_limit"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "limit"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**payload, "agent_id": "sub"})))
    assert cli.main(["hook", "limit"]) == 0
    assert calls == ["/api/claude/sessions/7/limit"]  # a subagent's limit is not the session's


def test_limited_session_resumes_after_the_reset(env, monkeypatch):
    tid, sess = _spawned()
    reset = time.time() + 3600
    monkeypatch.setattr(usage, "limit_reset", lambda: reset)
    body = env.post(f"/api/claude/sessions/{tid}/limit").json()
    assert _epoch(body["resume_at"]) == pytest.approx(reset + claude_runner.RESUME_GRACE_SECONDS, abs=1)
    assert sess.limited and sess.hook_waiting and not sess.turn_over
    assert claude_runner.idle_session_ids(claude_runner.session_states()) == []  # no messages typed in

    written: list[bytes] = []

    async def fake_write(s, data):
        written.append(data)

    monkeypatch.setattr(claude_runner, "_write_pty", fake_write)
    assert asyncio.run(claude_runner.resume_limited()) == []  # not yet
    sess.resume_at = time.time() - claude_runner.RESUME_GRACE_SECONDS - 1
    assert asyncio.run(claude_runner.resume_limited()) == [tid]
    assert written == [claude_runner.RESUME_PROMPT.encode(), b"\r"]
    assert not sess.limited and sess.hook_waiting is False
    assert asyncio.run(claude_runner.resume_limited()) == []  # only once


def test_unknown_reset_waits_for_the_user(env, monkeypatch):
    tid, sess = _spawned()
    monkeypatch.setattr(usage, "limit_reset", lambda: None)
    assert env.post(f"/api/claude/sessions/{tid}/limit").json() == {"resume_at": None}
    assert sess.limited and asyncio.run(claude_runner.resume_limited()) == []


def test_reply_clears_the_limit(env, monkeypatch):
    tid, sess = _spawned()
    claude_runner.set_limited(tid, time.time())
    claude_runner.set_hook_state(tid, False)  # the user replied by hand
    assert not sess.limited and claude_runner.limit_resume_at(tid) is None


def test_limit_without_live_session_is_404(env, monkeypatch):
    monkeypatch.setattr(usage, "limit_reset", lambda: None)
    assert env.post("/api/claude/sessions/999/limit").status_code == 404


def test_limit_holds_back_new_claude_runs(env, monkeypatch):
    monkeypatch.setattr(taskqueue, "terminal_available", lambda spec: (True, None))
    tid, _ = _spawned()
    with get_conn() as conn:
        queued = conn.execute("INSERT INTO tasks (title, project) VALUES ('Q', 'p')").lastrowid
        other = conn.execute("INSERT INTO tasks (title, project, agent) VALUES ('O', 'q', 'opencode')").lastrowid
    taskqueue.set_queue([queued, other])
    assert "claude" in taskqueue._runnable_agents()

    claude_runner.set_limited(tid, time.time() + 3600)
    assert "claude" not in taskqueue._runnable_agents() and "opencode" in taskqueue._runnable_agents()
    skipped = taskqueue.skipped(taskqueue.load_queue(), {tid})
    assert skipped[queued]["reason"] == "limit" and other not in skipped
    assert _epoch(skipped[queued]["until"]) == pytest.approx(claude_runner.limit_hold_until(), abs=1)

    claude_runner.set_limited(tid, None)  # an unknown reset never shortens a known one
    assert claude_runner.limit_hold_until() is not None
    monkeypatch.setattr(claude_runner, "LIMIT_UNTIL", time.time() - claude_runner.RESUME_GRACE_SECONDS - 1)
    assert "claude" in taskqueue._runnable_agents()
    assert "limit" not in {v["reason"] for v in taskqueue.skipped(taskqueue.load_queue(), {tid}).values()}
