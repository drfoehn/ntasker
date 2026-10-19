"""Messages between agent sessions: broker, events, delivery, peers, conversation view."""

from __future__ import annotations

import asyncio
import io
import json
import os

import pytest
from fastapi.testclient import TestClient

from ntasker import claude_runner, cli, messages, sessions
from ntasker.app import app
from ntasker.db import get_conn, init_db, set_db_path
from ntasker.transcript import parse_transcript

SOCK = "/run/user/1000/cc-socks/4242.sock"


class _Proc:
    pid = os.getpid()


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    claude_runner.SESSIONS.clear()
    claude_runner.EXTERNAL_SOCKETS.clear()
    yield TestClient(app, base_url="http://127.0.0.1:8766")
    claude_runner.SESSIONS.clear()
    claude_runner.EXTERNAL_SOCKETS.clear()


def _add(title, project="p"):
    with get_conn() as conn:
        return conn.execute("INSERT INTO tasks (title, project) VALUES (?, ?)", (title, project)).lastrowid


def _rows(to_task):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM messages WHERE to_task = ? ORDER BY id", (to_task,)).fetchall()


def _spawned(task_id, socket=None):
    sess = claude_runner.TermSession(task_id=task_id, proc=_Proc(), master_fd=-1)  # type: ignore[arg-type]
    sess.socket = socket
    claude_runner.SESSIONS[task_id] = sess
    return sess


def test_post_refuses_bad_messages(env):
    a, b = _add("A"), _add("B")
    with get_conn() as conn:
        for kwargs, status in (
            ({"to_task": a, "body": "hi", "from_task": a}, 400),
            ({"to_task": a, "body": "  "}, 400),
            ({"to_task": 999, "body": "hi"}, 404),
        ):
            with pytest.raises(messages.MessageError) as exc:
                messages.post(conn, **kwargs)
            assert exc.value.status == status
        for _ in range(messages.PAIR_LIMIT):
            messages.post(conn, b, "ok", from_task=a)
        with pytest.raises(messages.MessageError) as exc:
            messages.post(conn, b, "ok", from_task=a)
        assert exc.value.status == 429
        messages.post(conn, b, "ntasker itself is not limited", kind="unblocked", from_task=a)


def test_send_and_take_over_the_api(env):
    a, b = _add("Alpha"), _add("Beta")
    r = env.post(f"/api/tasks/{b}/messages", json={"body": "repo X is mine until noon", "from_task": a})
    assert r.status_code == 201 and r.json()["live"] is False
    took = env.post(f"/api/tasks/{b}/messages/take").json()
    assert f'[nTasker message #{r.json()["id"]} from task #{a} "Alpha"]' in took["text"]
    assert "repo X is mine until noon" in took["text"] and messages.FOOTER in took["text"]
    assert _rows(b)[0]["delivered_via"] == "inbox"
    assert env.post(f"/api/tasks/{b}/messages/take").json()["messages"] == []
    assert [m["id"] for m in env.get(f"/api/tasks/{a}/messages").json()] == [r.json()["id"]]


def test_sender_resolved_from_external_pid(env):
    a, b = _add("A"), _add("B")
    claude_runner.register_external(a, os.getpid())
    r = env.post(f"/api/tasks/{b}/messages", json={"body": "hi", "from_pid": os.getpid()})
    assert r.json()["from_task"] == a


def test_wait_for_adds_dependency_and_tells_the_holder(env):
    a, b = _add("Waiter"), _add("Holder")
    r = env.post(f"/api/tasks/{a}/wait", json={"on": b, "why": "I need repo X"})
    assert r.status_code == 201
    assert [d["id"] for d in env.get(f"/api/tasks/{a}").json()["depends"]] == [b]
    (msg,) = _rows(b)
    assert msg["kind"] == "wait" and msg["from_task"] == a
    assert "I need repo X" in msg["body"] and f"ntasker finish {b}" in msg["body"]
    # The reverse would be a cycle -- a deadlock.
    assert env.post(f"/api/tasks/{b}/wait", json={"on": a}).status_code == 409


def test_finishing_wakes_live_dependents_once(env):
    a, b, idle = _add("Waiter"), _add("Holder"), _add("Not running")
    env.post(f"/api/tasks/{a}/wait", json={"on": b})
    env.patch(f"/api/tasks/{idle}", json={"depends": [b]})
    claude_runner.register_external(a, os.getpid())
    env.patch(f"/api/tasks/{b}", json={"report": "## Done\nAll good"})
    env.patch(f"/api/tasks/{b}", json={"phase": "review"})
    env.patch(f"/api/tasks/{b}", json={"phase": "review"})
    (msg,) = _rows(a)
    assert msg["kind"] == "unblocked" and f"#{b} Holder is finished (review)" in msg["body"]
    assert "Done" in msg["body"]
    assert _rows(idle) == []


def test_done_after_review_releases_the_project_lock(env, monkeypatch):
    monkeypatch.setattr("ntasker.app.stop_session", lambda tid: None)  # the fake session is pytest itself
    a, b = _add("Waiter"), _add("Holder", project="thrito")
    env.post(f"/api/tasks/{a}/wait", json={"on": b})
    claude_runner.register_external(a, os.getpid())
    _spawned(b)
    env.patch(f"/api/tasks/{b}", json={"phase": "review"})
    (review,) = _rows(a)
    assert "thrito stays locked" in review["body"]
    env.patch(f"/api/tasks/{b}", json={"status": "done"})
    env.patch(f"/api/tasks/{b}", json={"status": "done"})
    assert [m["kind"] for m in _rows(a)] == ["unblocked", "lock"]
    assert "lock on thrito is released" in _rows(a)[1]["body"]


def test_cli_done_is_noticed_by_the_queue_sweep(env, monkeypatch):
    from ntasker import taskqueue

    a, b = _add("Waiter"), _add("Holder", project="thrito")
    env.post(f"/api/tasks/{a}/wait", json={"on": b})
    claude_runner.register_external(a, os.getpid())
    _spawned(b)
    env.patch(f"/api/tasks/{b}", json={"phase": "review"})
    with get_conn() as conn:  # ``ntasker done`` writes the DB directly
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (b,))
    stopped = []
    monkeypatch.setattr(taskqueue, "stop_session", stopped.append)
    taskqueue._kill_done({b}, {a})
    taskqueue._kill_done({b}, {a})
    assert stopped == [b, b]
    assert [m["kind"] for m in _rows(a)] == ["unblocked", "lock"]


def test_finish_without_success_reports_released_locks(env):
    a, b = _add("Waiter"), _add("Holder")
    env.patch(f"/api/tasks/{a}", json={"depends": [b]})
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET locks = ? WHERE id = ?", ('["other"]', b))
    _spawned(a)
    env.post(f"/api/tasks/{b}/outcome", json={"status": "blocked", "report": "stuck"})
    (msg,) = _rows(a)
    assert msg["kind"] == "lock" and "other" in msg["body"]


def test_lock_rm_tells_live_dependents(env):
    a, b = _add("Waiter"), _add("Holder")
    env.patch(f"/api/tasks/{a}", json={"depends": [b]})
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET locks = ? WHERE id = ?", ('["other"]', b))
    _spawned(a)
    env.delete(f"/api/tasks/{b}/locks/unknown")
    assert _rows(a) == []
    env.delete(f"/api/tasks/{b}/locks/other")
    assert [m["kind"] for m in _rows(a)] == ["lock"]


def test_external_session_gets_messages_through_its_hook(env):
    a = _add("A")
    claude_runner.register_external(a, os.getpid())
    env.post(f"/api/tasks/{a}/messages", json={"body": "hello"})
    sid = "12345678-1234-1234-1234-123456789abc"
    body = {"session_id": sid, "pid": os.getpid(), "cwd": "/p", "socket": SOCK}
    first = env.post("/api/claude/sessions/live", json=body).json()
    assert "hello" in first["messages"]
    assert env.post("/api/claude/sessions/live", json=body).json()["messages"] is None
    assert sessions.LIVE[sid].socket == SOCK


def test_deliver_types_into_idle_sessions_only(env, monkeypatch):
    a, b = _add("A"), _add("B")
    idle, busy = _spawned(a), _spawned(b)
    idle.hook_waiting, idle.turn_over = True, True
    busy.hook_waiting = False
    written: list[tuple[int, bytes]] = []

    async def fake_write(sess, data):
        written.append((sess.task_id, data))

    monkeypatch.setattr(claude_runner, "_write_pty", fake_write)
    env.post(f"/api/tasks/{a}/messages", json={"body": "for a"})
    env.post(f"/api/tasks/{b}/messages", json={"body": "for b"})
    asyncio.run(messages.deliver())
    assert [tid for tid, _ in written] == [a, a]
    paste, enter = written[0][1].decode(), written[1][1]
    assert paste.startswith("\x1b[200~[nTasker message #") and "for a" in paste and enter == b"\r"
    assert _rows(a)[0]["delivered_via"] == "pty" and _rows(b)[0]["delivered_at"] is None
    assert idle.turn_over is False and idle.hook_waiting is False
    asyncio.run(messages.deliver())
    assert len(written) == 2   # a is busy now, b still works


def test_peers_lists_addresses(env):
    a, b = _add("Spawned"), _add("External")
    _spawned(a, SOCK)
    claude_runner.register_external(b, os.getpid(), "/s/ext.sock")
    sessions.register_live("12345678-1234-1234-1234-123456789abc", os.getpid(), "/p/free", "/s/free.sock")
    data = env.get("/api/peers").json()
    by_id = {t["id"]: t for t in data["tasks"]}
    assert by_id[a]["address"] == f"uds:{SOCK}" and by_id[a]["external"] is False
    assert by_id[b]["address"] == "uds:/s/ext.sock" and by_id[b]["external"] is True
    assert [s["address"] for s in data["sessions"]] == ["uds:/s/free.sock"]
    assert messages.address_names()[f"uds:{SOCK}"] == f"#{a} Spawned"


def test_hook_state_reports_the_socket(env, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_api_call", lambda base, method, path, body=None, timeout=5.0: calls.append(body) or (200, {}))
    monkeypatch.setenv("NTASKER_TASK_ID", "7")
    monkeypatch.setenv("NTASKER_URL", "http://127.0.0.1:8766")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", SOCK)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    cli.main(["hook", "running"])
    assert calls[0]["socket"] == SOCK
    a = _add("A")
    sess = _spawned(a)
    claude_runner.set_hook_state(a, False, socket=SOCK)
    assert sess.socket == SOCK


def test_hook_session_prints_messages_as_context(monkeypatch, capsys):
    answer = {"ok": True, "messages": "[nTasker message #1 from nTasker]\nhi"}
    monkeypatch.setattr(cli, "_api_call", lambda *a, **kw: (200, answer))
    monkeypatch.delenv("NTASKER_TASK_ID", raising=False)
    monkeypatch.setenv("CLAUDE_PID", "4242")
    payload = {"session_id": "12345678-1234-1234-1234-123456789abc", "hook_event_name": "UserPromptSubmit"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["hook", "session"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"] == {"hookEventName": "UserPromptSubmit", "additionalContext": answer["messages"]}


def test_msg_cli_sends_as_own_task(monkeypatch, capsys):
    calls = []

    def api_call(base, method, path, body=None, timeout=5.0):
        calls.append((method, path, body))
        return 201, {"id": 3, "live": True}

    monkeypatch.setattr(cli, "_api_call", api_call)
    monkeypatch.setenv("NTASKER_TASK_ID", "5")
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    assert cli.main(["msg", "#8", "done", "with", "X"]) == 0
    assert calls == [("POST", "/api/tasks/8/messages", {"body": "done with X", "from_task": 5, "from_pid": None})]
    assert cli.main(["wait-for", "8", "need", "repo"]) == 0
    assert calls[-1] == ("POST", "/api/tasks/5/wait", {"on": 8, "why": "need repo"})


def test_seed_carries_the_messaging_rules(env):
    a = _add("A")
    seed = claude_runner.queue_seed_for_task({"id": a, "title": "A", "project": "p"})
    assert claude_runner.MESSAGING_RULES.replace("{id}", str(a)) in seed


def _ev(**kw):
    return json.dumps(kw)


def test_conversation_marks_agent_messages():
    peer_origin = {"kind": "peer", "from": f"uds:{SOCK}", "name": "other-7", "body": "ack"}
    broker = "[nTasker message #4 from task #2 \"B\"]\nI am done\n\n" + messages.FOOTER
    lines = [
        _ev(type="user", message={"role": "user", "content": "start"}, timestamp="t1"),
        _ev(type="assistant", message={"id": "m1", "role": "assistant", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "u1", "name": "SendMessage", "input": {"to": f"uds:{SOCK}", "message": "hi"}},
            {"type": "tool_use", "id": "u2", "name": "Bash", "input": {"command": 'ntasker msg 2 "please hurry"'}},
            {"type": "tool_use", "id": "u3", "name": "Bash", "input": {"command": "ntasker wait-for 2 repo X"}},
        ]}, timestamp="t2"),
        _ev(type="attachment", attachment={"type": "queued_command", "commandMode": "prompt",
                                           "prompt": "x", "origin": peer_origin}, timestamp="t3"),
        _ev(type="user", isMeta=True, message={"role": "user", "content": "Another Claude session ..."},
            origin=peer_origin, timestamp="t4"),
        _ev(type="user", message={"role": "user", "content": broker}, timestamp="t5"),
        _ev(type="user", message={"role": "user", "content":
            f'<pasted_content id="ab12">\n{broker}\n</pasted_content id="ab12">'}, timestamp="t6"),
    ]
    first, woken, brokered, pasted = parse_transcript(lines)
    assert pasted["peer"] == brokered["peer"] and pasted["prompt"] == brokered["prompt"]
    assert first["peer"] is None
    assert [(m["to"], m["text"], m["kind"], m["via"]) for m in first["sent"]] == [
        (f"uds:{SOCK}", "hi", "note", "claude"),
        ("#2", "please hurry", "note", "ntasker"),
        ("#2", "repo X", "wait", "ntasker"),
    ]
    assert first["injected"] == [{"text": "ack", "at": "t3", "peer": "other-7"}]
    assert woken["peer"] == {"name": "other-7", "via": "claude"} and woken["prompt"] == "ack"
    assert brokered["peer"] == {"name": 'task #2 "B"', "via": "ntasker"}
    assert brokered["prompt"] == "I am done"


def test_fasttrack_change_goes_into_a_working_session_at_once(env, monkeypatch):
    a = _add("A")
    busy = _spawned(a)
    busy.hook_waiting = False   # mid-turn, by its hooks' word
    written: list[bytes] = []

    async def fake_write(sess, data):
        written.append(data)

    monkeypatch.setattr(claude_runner, "_write_pty", fake_write)
    env.patch(f"/api/tasks/{a}", json={"fasttrack": True})
    env.patch(f"/api/tasks/{a}", json={"fasttrack": True})   # unchanged: no second note
    asyncio.run(messages.deliver())
    paste = written[0].decode()
    assert "from nTasker]" in paste and f"ntasker finish {a} --status ok" in paste
    assert messages.FOOTER not in paste
    assert [r["delivered_via"] for r in _rows(a)] == ["pty"]
    assert busy.hook_waiting is False   # still working -- not marked busy anew

    env.patch(f"/api/tasks/{a}", json={"fasttrack": False})
    asyncio.run(messages.deliver())
    assert "removed the fasttrack flag" in written[2].decode()


def test_fasttrack_note_waits_out_a_dialog_and_skips_dead_tasks(env, monkeypatch):
    a, b = _add("A"), _add("B")
    asking = _spawned(a)
    asking.hook_waiting = True   # a permission dialog: typing would answer it
    monkeypatch.setattr(claude_runner, "_write_pty", lambda sess, data: pytest.fail("typed into a dialog"))
    env.patch(f"/api/tasks/{a}", json={"fasttrack": True})
    env.patch(f"/api/tasks/{a}", json={"fasttrack": False})   # supersedes the first
    env.patch(f"/api/tasks/{b}", json={"fasttrack": True})    # no session at all
    asyncio.run(messages.deliver())
    assert [r["delivered_via"] for r in _rows(a)] == ["superseded", None]
    assert _rows(b)[0]["delivered_via"] == "dropped"


def test_cli_fasttrack_change_queues_the_note(env, capsys):
    from ntasker import db

    a = _add("A")
    argv = ["--db", str(db.DB_PATH), "patch", str(a), "--fasttrack"]
    assert cli.main(argv) == 0
    assert cli.main(argv) == 0
    assert [r["kind"] for r in _rows(a)] == ["fasttrack"]
