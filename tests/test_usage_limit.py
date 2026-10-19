"""Usage limits: a stopped run is paused (``tasks.limit_until``) and resumed after the reset."""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from ntasker import app as app_module
from ntasker import claude_runner, cli, plugins, taskqueue, usagelimit
from ntasker.agents import AGENTS
from ntasker.app import app
from ntasker.claude_assets import hooks_settings_path
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.plugins.claude import usage


class _Proc:
    pid = os.getpid()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(plugins.ENV_DISABLED, raising=False)
    for var in ("NTASKER_DIR_LOCKS", "NTASKER_QUEUE_ENABLED", "NTASKER_TASK_ID", "NTASKER_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(AGENTS["claude"].home_env, str(tmp_path / "claude"))
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    claude_runner.SESSIONS.clear()
    # No subscription unless a test says otherwise.
    monkeypatch.setattr(usage, "snapshot", lambda force=False: None)
    monkeypatch.setattr(app_module, "changed_paths", lambda b: [])
    monkeypatch.setattr(taskqueue, "changed_paths", lambda b: [])
    for reg in (taskqueue._running, taskqueue.RESUME, taskqueue.LIMIT_RESUME, taskqueue.QUICK):
        reg.clear()
    taskqueue._booted = True
    yield TestClient(app, base_url="http://127.0.0.1:8766")
    claude_runner.SESSIONS.clear()


def _task(project="p", **cols):
    cols = {"title": "T", "project": project, "session_id": str(uuid.uuid4()), **cols}
    with get_conn() as conn:
        return conn.execute(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values())
        ).lastrowid


def _spawned(**cols):
    tid = _task(**cols)
    sess = claude_runner.TermSession(task_id=tid, proc=_Proc(), master_fd=-1)  # type: ignore[arg-type]
    claude_runner.SESSIONS[tid] = sess
    return tid, sess


def _col(task_id, name):
    with get_conn() as conn:
        return conn.execute(f"SELECT {name} FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _transcript(tmp_path, session_id, events):
    d = tmp_path / "claude" / "projects" / "-p"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


WRAP_UP = {"type": "user", "isMeta": True, "usageLimitNote": "wrap_up", "timestamp": "2026-10-18T22:18:08Z",
           "message": {"role": "user", "content": "[Usage limit reached; ...]"}}
RELEASE = {"type": "user", "isMeta": True, "usageLimitNote": "release",
           "message": {"role": "user", "content": "[Earlier usage-limit notes no longer apply.]"}}


def _reply(text="ok", **extra):
    return {"type": "assistant", "isSidechain": False, "timestamp": "2026-10-18T22:19:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, **extra}


def _limit_error(text="You've hit your session limit · resets 3pm"):
    return _reply(text, isApiErrorMessage=True, error="rate_limit")


def _set_due(task_id):
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET limit_until = ? WHERE id = ?", (usagelimit._iso(time.time() - 1), task_id))


async def _no_write(sess, data):
    return None


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


# --- detection ------------------------------------------------------------------


def test_transcript_limit_stop(env, tmp_path):
    sid = str(uuid.uuid4())
    home = tmp_path / "claude"
    _transcript(tmp_path, sid, [_reply("working")])
    assert usagelimit.detect(sid) is None
    # the grace allowance: the agent wraps up, the note stays in force
    _transcript(tmp_path, sid, [_reply(), WRAP_UP, _reply("wrapped up")])
    assert usagelimit.detect(sid) == {"text": "", "at": WRAP_UP["timestamp"]}
    _transcript(tmp_path, sid, [WRAP_UP, _reply(), RELEASE, _reply()])
    assert usagelimit.detect(sid) is None                  # the limit reset, the user went on
    # a turn cut off: the API error names the reset; a later reply recovered
    _transcript(tmp_path, sid, [_reply(), _limit_error()])
    assert usagelimit.detect(sid)["text"].endswith("resets 3pm")
    _transcript(tmp_path, sid, [_limit_error(), _reply()])
    assert usagelimit.detect(sid) is None
    _transcript(tmp_path, sid, [{**WRAP_UP, "isSidechain": True}])
    assert usagelimit.detect(sid) is None                  # a subagent's note is not the session's
    assert home.is_dir() and usagelimit.detect(None) is None


@pytest.mark.parametrize(
    ("text", "hour", "minute"),
    [("resets 3pm", 15, 0), ("resets 3:30am (Europe/Vienna)", 3, 30), ("resets 15:00", 15, 0), ("resets 12am", 0, 0)],
)
def test_reset_from_text(text, hour, minute):
    now = datetime(2026, 10, 18, 22, 18).timestamp()
    at = datetime.fromtimestamp(usagelimit.reset_from_text(text, now))
    assert (at.hour, at.minute) == (hour, minute) and at.timestamp() > now
    assert at.timestamp() - now < 24 * 3600


def test_reset_from_text_unknown():
    assert usagelimit.reset_from_text("no time here") is None
    assert usagelimit.reset_from_text("") is None


def test_stopfailure_pauses_the_run(env, monkeypatch):
    tid, sess = _spawned()
    reset = time.time() + 3600
    monkeypatch.setattr(usage, "limit_reset", lambda: reset)
    body = env.post(f"/api/claude/sessions/{tid}/limit").json()
    assert _epoch(body["resume_at"]) == pytest.approx(reset + usagelimit.GRACE_SECONDS, abs=1)
    assert _epoch(_col(tid, "limit_until")) == pytest.approx(reset + usagelimit.GRACE_SECONDS, abs=1)
    assert sess.hook_waiting and not sess.turn_over
    assert claude_runner.idle_session_ids(claude_runner.session_states()) == []  # no messages typed in
    assert env.get(f"/api/tasks/{tid}").json()["limit_until"] == _col(tid, "limit_until")


def test_unknown_reset_polls(env, monkeypatch, tmp_path):
    tid, _ = _spawned()
    monkeypatch.setattr(usage, "limit_reset", lambda: None)
    body = env.post(f"/api/claude/sessions/{tid}/limit").json()
    assert _epoch(body["resume_at"]) == pytest.approx(time.time() + usagelimit.POLL_SECONDS, abs=2)


def test_stopfailure_reset_from_the_message(env, monkeypatch, tmp_path):
    tid, _ = _spawned()
    monkeypatch.setattr(usage, "limit_reset", lambda: None)
    _transcript(tmp_path, _col(tid, "session_id"), [_limit_error("limit · resets 3pm")])
    at = datetime.fromtimestamp(_epoch(env.post(f"/api/claude/sessions/{tid}/limit").json()["resume_at"]))
    assert (at.hour, at.minute) == (15, 1)  # 3pm + the grace minute


def test_limit_without_live_session_is_404(env):
    assert env.post("/api/claude/sessions/999/limit").status_code == 404


def test_stop_hook_pauses_a_wrapped_up_turn(env, tmp_path):
    tid, sess = _spawned()
    sid = _col(tid, "session_id")
    _transcript(tmp_path, sid, [WRAP_UP, _reply("limit reached, stopping")])
    assert env.post(f"/api/claude/sessions/{tid}/state", json={"waiting": True, "turn_over": True}).status_code == 200
    assert _col(tid, "limit_until") and not sess.turn_over
    # the limit reset and the user carried on: the next ordinary turn ends the pause
    _transcript(tmp_path, sid, [WRAP_UP, _reply(), RELEASE, _reply()])
    env.post(f"/api/claude/sessions/{tid}/state", json={"waiting": True, "turn_over": True})
    assert _col(tid, "limit_until") is None and sess.turn_over


def test_stop_hook_leaves_a_handed_off_task(env, tmp_path):
    tid, _ = _spawned(phase="review")
    _transcript(tmp_path, _col(tid, "session_id"), [WRAP_UP, _reply("done in time")])
    env.post(f"/api/claude/sessions/{tid}/state", json={"waiting": True, "turn_over": True})
    assert _col(tid, "limit_until") is None


# --- finish: the agent reports the limit itself ------------------------------------


def test_finish_blocked_under_a_limit_pauses(env, tmp_path):
    """#1112: a fasttrack agent finished ``blocked`` within its grace allowance."""
    tid, _ = _spawned(fasttrack=1, fail_continue=1, phase="wip", locks='["other"]')
    taskqueue.set_queue([tid])
    _transcript(tmp_path, _col(tid, "session_id"), [WRAP_UP, _reply()])
    r = env.post(f"/api/tasks/{tid}/outcome", json={"status": "blocked", "summary": "Nutzungslimit erreicht",
                                                     "report": "## Done\nBaseline"})
    assert r.status_code == 200 and r.json()["limit_until"] and r.json()["outcome_id"] is None
    assert _col(tid, "report") == "## Done\nBaseline"
    with get_conn() as conn:  # nTasker's status, not the agent's ``blocked``
        logged = [dict(r) for r in conn.execute("SELECT status, summary, report FROM run_outcomes")]
    assert logged == [{"status": "limit", "summary": logged[0]["summary"], "report": "## Done\nBaseline"}]
    assert logged[0]["summary"].startswith("usage limit reached -- paused until ")
    assert taskqueue.load_queue()[0]["id"] == tid       # fail_continue does not drop it
    assert _col(tid, "phase") == "wip" and _col(tid, "status") == "open"
    assert _col(tid, "locks") == '["other"]'           # it goes on: the locks stay


def test_finish_blocked_without_a_limit_stays_blocked(env, tmp_path):
    tid, _ = _spawned(fasttrack=1)
    _transcript(tmp_path, _col(tid, "session_id"), [_reply("the API key is missing")])
    r = env.post(f"/api/tasks/{tid}/outcome", json={"status": "blocked", "summary": "API key missing"})
    assert r.json()["limit_until"] is None and r.json()["outcome_id"]
    with get_conn() as conn:
        assert conn.execute("SELECT status FROM run_outcomes").fetchone()[0] == "blocked"


def test_finish_ok_ends_a_pause(env, monkeypatch):
    monkeypatch.setattr(app_module, "stop_session", lambda tid: True)
    tid, _ = _spawned()
    usagelimit.park(tid, time.time() + 600)
    env.post(f"/api/tasks/{tid}/outcome", json={"status": "ok"})
    assert _col(tid, "limit_until") is None


def test_cli_finish_says_paused(monkeypatch, capsys):
    answer = {"limit_until": "2026-10-19T04:01:00+02:00"}
    monkeypatch.setattr(cli, "_api_call", lambda base, method, path, body=None, timeout=5.0: (200, answer))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["finish", "5", "--status", "blocked", "--summary", "limit"]) == 0
    assert "2026-10-19T04:01:00+02:00" in capsys.readouterr().out


# --- queue: hold, persistence, resume ---------------------------------------------


def test_pause_holds_back_new_claude_runs(env, monkeypatch):
    monkeypatch.setattr(taskqueue, "terminal_available", lambda spec: (True, None))
    tid, _ = _spawned()
    queued = _task("p2")
    other = _task("q", agent="opencode")
    taskqueue.set_queue([queued, other])
    assert "claude" in taskqueue._runnable_agents()

    usagelimit.park(tid, time.time() + 3600)
    assert "claude" not in taskqueue._runnable_agents() and "opencode" in taskqueue._runnable_agents()
    skipped = taskqueue.skipped(taskqueue.load_queue(), {tid})
    assert skipped[queued]["reason"] == "limit" and other not in skipped
    assert _epoch(skipped[queued]["until"]) == pytest.approx(usagelimit.hold_until(), abs=1)

    _set_due(tid)
    assert usagelimit.hold_until() is None and "claude" in taskqueue._runnable_agents()


def test_live_session_resumes_after_the_reset(env, monkeypatch):
    tid, sess = _spawned()
    usagelimit.park(tid, time.time() + 3600)
    written: list[bytes] = []

    async def fake_write(s, data):
        written.append(data)

    monkeypatch.setattr(taskqueue, "_write_pty", fake_write)
    assert asyncio.run(taskqueue.resume_paused()) == []   # not yet
    _set_due(tid)
    assert asyncio.run(taskqueue.resume_paused()) == [tid]
    assert written == [usagelimit.RESUME_PROMPT.encode(), b"\r"]
    assert _col(tid, "limit_until") is None and sess.hook_waiting is False
    assert asyncio.run(taskqueue.resume_paused()) == []   # only once


def test_still_full_window_postpones(env, monkeypatch):
    tid, _ = _spawned()
    later = time.time() + 7200
    monkeypatch.setattr(usage, "snapshot", lambda force=False: {
        "five_hour": {"utilization": 100.0, "resets_at": usagelimit._iso(later)}, "seven_day": None})
    monkeypatch.setattr(taskqueue, "_write_pty", _no_write)
    _set_due(tid)
    assert asyncio.run(taskqueue.resume_paused()) == []
    assert _epoch(_col(tid, "limit_until")) == pytest.approx(later + usagelimit.GRACE_SECONDS, abs=1)


def test_paused_runs_survive_a_restart_and_all_resume(env, monkeypatch):
    """Three paused runs, the server restarted in between: none starts early, all go on after the reset."""
    monkeypatch.setattr(taskqueue, "terminal_available", lambda spec: (True, None))
    started: list[tuple[int, str | None, bool]] = []
    live: set[int] = set()
    monkeypatch.setattr(taskqueue, "active_session_ids", lambda: list(live))

    def fake_start(task_id, seed, quick=False, resume=False):
        started.append((task_id, seed, resume))
        live.add(task_id)
        return True

    monkeypatch.setattr(taskqueue, "start_detached_session", fake_start)
    a, b, c = _task("x"), _task("x"), _task("y")
    nxt = _task("z", session_id=None)
    taskqueue.set_queue([a, b, c, nxt])
    reset = time.time() + 3600
    with get_conn() as conn:  # what the previous process left behind
        conn.execute("UPDATE tasks SET session_ended_at = '2026-10-18T22:20:00' WHERE id IN (?, ?, ?)", (a, b, c))
    for tid in (a, b, c):
        usagelimit.park(tid, reset)

    taskqueue._booted = False
    taskqueue.tick()                                      # first tick after the restart
    assert started == [] and not taskqueue.RESUME         # no early resume, no new Claude run
    reasons = taskqueue.skipped(taskqueue.load_queue(), live)
    assert {reasons[t]["reason"] for t in (a, b, c, nxt)} == {"limit"}

    for tid in (a, b, c):
        _set_due(tid)
    assert asyncio.run(taskqueue.resume_paused()) == [a, b, c]   # lane order
    taskqueue.tick()
    # one per lane: a and c resume with the prompt, b waits behind a; nxt starts fresh in its own lane
    by = {t: (seed, resume) for t, seed, resume in started}
    assert by[a] == by[c] == (usagelimit.RESUME_PROMPT, True)
    assert by[nxt][1] is False and b not in by
    live.discard(a)
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (a,))
    taskqueue.tick()
    assert (b, usagelimit.RESUME_PROMPT, True) in started
    assert all(_col(t, "limit_until") is None for t in (a, b, c))


def test_resume_with_prompt_argv():
    spec = AGENTS["claude"]
    args = spec.build_spawn("go on", resume_id="abc", session_id="ignored", system_prompt="ignored")
    assert args[-3:] == [spec.resume_flag, "abc", "go on"] and "ignored" not in args
    assert spec.build_spawn(None, resume_id="abc")[-2:] == [spec.resume_flag, "abc"]


def test_seed_rules_tell_the_agent_not_to_finish_on_a_limit():
    from ntasker.settings import FASTTRACK_RULES_DEFAULT, RUN_RULES_DEFAULT, RUN_RULES_NO_REPORT_DEFAULT

    for rules in (RUN_RULES_DEFAULT, RUN_RULES_NO_REPORT_DEFAULT):
        assert "usage limit is no blocker" in rules
    assert "a usage limit is none" in FASTTRACK_RULES_DEFAULT


def test_detected_limit_logs_once(env, monkeypatch, tmp_path):
    """The agent is cut off and reports nothing: nTasker logs ``limit`` itself, once per pause."""
    tid, _ = _spawned(fasttrack=1)
    plain, _ = _spawned()
    monkeypatch.setattr(usage, "limit_reset", lambda: time.time() + 3600)
    _transcript(tmp_path, _col(tid, "session_id"), [_limit_error("limit · resets 3pm")])
    for t in (tid, plain):
        env.post(f"/api/claude/sessions/{t}/limit")
    env.post(f"/api/claude/sessions/{tid}/limit")           # a second report: same pause
    _set_due(tid)
    monkeypatch.setattr(usage, "snapshot", lambda force=False: {
        "five_hour": {"utilization": 100.0, "resets_at": usagelimit._iso(time.time() + 600)}, "seven_day": None})
    asyncio.run(taskqueue.resume_paused())                  # postponed: no new row either
    with get_conn() as conn:
        rows = conn.execute("SELECT task_id, status, summary FROM run_outcomes").fetchall()
    assert [(r["task_id"], r["status"]) for r in rows] == [(tid, "limit")]  # plain tasks have no run log
    assert rows[0]["summary"].endswith("(limit · resets 3pm)")
