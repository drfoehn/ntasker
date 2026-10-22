"""Tests for the task-context plugin (attachments, picker endpoints, hooks)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ntasker import plugins
from ntasker.app import app
from ntasker.db import init_db, set_db_path
from ntasker.plugins.task_context import context_briefing
from ntasker.plugins.workspace import scan

BASE = "http://127.0.0.1:8766"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(plugins.ENV_DISABLED, raising=False)
    path = tmp_path / "t.db"
    set_db_path(path)
    init_db(path)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def task(client):
    return client.post("/api/tasks", json={"title": "t"}).json()


def test_attach_file_roundtrip(client, task, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# hi\n")
    r = client.post(f"/api/tasks/{task['id']}/context", json={"kind": "file", "path": str(f)})
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["label"] == "notes.md" and entry["exists"] is True and entry["is_dir"] is False
    got = client.get(f"/api/tasks/{task['id']}").json()["context"]
    assert [c["id"] for c in got] == [entry["id"]]
    # list endpoint carries it too (bulk hook)
    listed = next(t for t in client.get("/api/tasks").json() if t["id"] == task["id"])
    assert listed["context"][0]["path"] == str(f)
    # re-attaching the same path updates the note instead of failing
    r = client.post(
        f"/api/tasks/{task['id']}/context", json={"kind": "file", "path": str(f), "note": "why"}
    )
    assert r.status_code == 201 and r.json()["id"] == entry["id"] and r.json()["note"] == "why"
    # preview reads the attached file; missing file reports exists=false
    assert client.get(f"/api/tasks/{task['id']}/context/{entry['id']}/file").json()["text"] == "# hi\n"
    f.unlink()
    assert client.get(f"/api/tasks/{task['id']}").json()["context"][0]["exists"] is False
    # detach, then 404 on repeat
    assert client.delete(f"/api/tasks/{task['id']}/context/{entry['id']}").status_code == 204
    assert client.delete(f"/api/tasks/{task['id']}/context/{entry['id']}").status_code == 404


def test_validation(client, task, tmp_path):
    tid = task["id"]
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "nope", "path": "/x"}).status_code == 400
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "brain", "path": "brain://x"}).status_code == 400
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "file", "path": "/no/such"}).status_code == 404
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "mcp", "path": "mcp://nope"}).status_code == 404
    # workspace kinds are confined to the configured roots
    f = tmp_path / "n.md"
    f.write_text("x")
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "note", "path": str(f)}).status_code == 403
    client.put("/api/settings/workspace_wiki_dir", json={"value": str(tmp_path)})
    r = client.post(f"/api/tasks/{tid}/context", json={"kind": "note", "path": str(f)})
    assert r.status_code == 201 and r.json()["label"] == "n"
    assert client.get("/api/tasks/999/context").status_code == 404


def test_create_with_context_is_atomic(client, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    before = len(client.get("/api/tasks").json())
    r = client.post("/api/tasks", json={"title": "x", "context": [{"kind": "file", "path": "/no/such"}]})
    assert r.status_code == 404
    assert len(client.get("/api/tasks").json()) == before
    r = client.post("/api/tasks", json={"title": "x", "context": [{"kind": "file", "path": str(f), "note": "n"}]})
    assert r.status_code == 201
    assert [(c["label"], c["note"]) for c in r.json()["context"]] == [("a.txt", "n")]


def test_fs_endpoints(client, monkeypatch, tmp_path):
    r = client.get("/api/fs/resolve", params={"path": str(tmp_path)})
    assert r.status_code == 200 and r.json()["is_dir"] is True
    assert client.get("/api/fs/resolve", params={"path": "/no/such"}).status_code == 404
    monkeypatch.setattr(scan, "picker_available", lambda: False)
    assert client.get("/api/fs/pick").json() == {"available": False}
    assert client.post("/api/fs/pick", json={"folder": False}).status_code == 501


def test_origin_guard_covers_context_writes(client, task, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    r = client.post(
        f"/api/tasks/{task['id']}/context",
        json={"kind": "file", "path": str(f)},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403


def test_disabled_plugin_hides_everything(client, task, tmp_path, monkeypatch):
    f = tmp_path / "a.txt"
    f.write_text("a")
    client.post(f"/api/tasks/{task['id']}/context", json={"kind": "file", "path": str(f)})
    monkeypatch.setenv(plugins.ENV_DISABLED, "task_context")
    assert client.get(f"/api/tasks/{task['id']}/context").status_code == 404
    assert "context" not in client.get(f"/api/tasks/{task['id']}").json()
    assert "task_context" not in client.get("/").text.split("window.__plugins = ")[1].split(";")[0]
    monkeypatch.delenv(plugins.ENV_DISABLED)
    # data survived the toggle
    assert len(client.get(f"/api/tasks/{task['id']}/context").json()) == 1


def test_briefing_lines(client, task, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("a")
    client.post(f"/api/tasks/{task['id']}/context", json={"kind": "file", "path": str(f), "note": "read me"})
    lines = plugins.run_briefings(task["id"])
    text = "\n".join(lines)
    assert "## Attached context" in text and str(f) in text and "read me" in text
    assert context_briefing([]) == []
    assert "file not found" in "\n".join(context_briefing([{"kind": "doc", "path": "/x", "label": "x", "exists": False}]))


def test_upload_pasted_image_and_attach(client, task, tmp_path, monkeypatch):
    import base64

    from ntasker.plugins.task_context import routes

    monkeypatch.setattr(routes, "uploads_dir", lambda: tmp_path / "uploads")
    data = base64.b64encode(b"\x89PNG fake").decode()
    r = client.post("/api/context/upload", json={"name": "../../evil.png", "data": data})
    assert r.status_code == 201, r.text
    path = r.json()["path"]
    assert path.startswith(str(tmp_path / "uploads")) and path.endswith("-evil.png")
    assert open(path, "rb").read() == b"\x89PNG fake"
    # attach it like any file -- what the paste handler does next
    r = client.post(f"/api/tasks/{task['id']}/context", json={"kind": "file", "path": path})
    assert r.status_code == 201 and r.json()["label"] == r.json()["path"].rsplit("/", 1)[-1]
    assert client.post("/api/context/upload", json={"name": "x.png", "data": "not base64!"}).status_code == 400
    assert client.post("/api/context/upload", json={"name": "x.png", "data": ""}).status_code == 413


def test_link_task(client, task):
    other = client.post("/api/tasks", json={"title": "background"}).json()
    tid = task["id"]
    for path in (f"task://{other['id']}", f"#{other['id']}", str(other["id"])):
        r = client.post(f"/api/tasks/{tid}/context", json={"kind": "task", "path": path})
        assert r.status_code == 201, r.text
        entry = r.json()
        assert entry["path"] == f"task://{other['id']}"
        assert entry["label"] == f"#{other['id']} background"
        assert entry["exists"] is True and entry["remote"] is True
    assert len(client.get(f"/api/tasks/{tid}/context").json()) == 1
    # self-link, unknown id, wrong scheme / kind combinations
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "task", "path": str(tid)}).status_code == 400
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "task", "path": "999999"}).status_code == 404
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "task", "path": "abc"}).status_code == 400
    assert client.post(f"/api/tasks/{tid}/context", json={"kind": "file", "path": "task://1"}).status_code == 400
    text = "\n".join(plugins.run_briefings(tid))
    assert "Linked task: #" in text and f"ntasker show {other['id']}" in text


def test_brief_for_running_session(client, task, tmp_path):
    tid = task["id"]
    f = tmp_path / "paper.pdf"
    f.write_bytes(b"%PDF-1.4")
    a = client.post(f"/api/tasks/{tid}/context", json={"kind": "file", "path": str(f), "note": "methods"}).json()
    b = client.post(f"/api/tasks/{tid}/context", json={"kind": "file", "path": str(tmp_path)}).json()
    r = client.post(f"/api/tasks/{tid}/context/brief", json={"ids": [a["id"], 424242]})
    assert r.status_code == 200
    text = r.json()["text"]
    assert text.startswith("Additional context for this task")
    assert str(f) in text and "methods" in text and "## Attached context" not in text
    assert str(tmp_path) + "`" not in text   # b was not asked for
    assert client.post(f"/api/tasks/{tid}/context/brief", json={"ids": []}).json()["text"] == ""
    assert client.post("/api/tasks/999999/context/brief", json={"ids": [b["id"]]}).status_code == 404


def test_conv_reply_slot_rendered(client, task):
    html = client.get("/").text
    assert "openLiveContext()" in html
    assert "convRefsBrief" in client.get("/static/plugins/task_context/task_context.js").text


def test_reply_file_is_stored_and_attached(client, task, tmp_path, monkeypatch):
    from ntasker.plugins.task_context import routes

    monkeypatch.setattr(routes, "uploads_dir", lambda: tmp_path / "uploads")
    path = plugins.store_upload(task["id"], "../paper.pdf", b"%PDF-1.4")
    assert path and path.startswith(str(tmp_path / "uploads")) and path.endswith("-paper.pdf")
    [entry] = client.get(f"/api/tasks/{task['id']}/context").json()
    assert entry["kind"] == "file" and entry["path"] == path and entry["label"] == "paper.pdf"
    # later runs get it in their briefing
    assert path in "\n".join(plugins.run_briefings(task["id"]))
    # plugin off: the core keeps the file in its temp dir, nothing attached
    monkeypatch.setenv(plugins.ENV_DISABLED, "task_context")
    assert plugins.store_upload(task["id"], "x.pdf", b"x") is None


def test_sweep_uploads(client, task, tmp_path, monkeypatch):
    import os

    from ntasker.plugins.task_context import routes

    monkeypatch.setattr(routes, "uploads_dir", lambda: tmp_path / "uploads")
    kept = routes.store_reply_upload(task["id"], "kept.pdf", b"k")
    other = client.post("/api/tasks", json={"title": "archived"}).json()
    released = routes.store_reply_upload(other["id"], "gone.pdf", b"g")
    orphan = str(routes.save_upload("orphan.png", b"o"))
    fresh = str(routes.save_upload("fresh.png", b"f"))   # create form, task not saved yet
    for p in (kept, released, orphan):
        os.utime(p, (0, 0))
    assert client.patch(f"/api/tasks/{other['id']}", json={"archived": True}).status_code == 200
    assert routes.sweep_uploads() == 2
    assert os.path.exists(kept) and os.path.exists(fresh)
    assert not os.path.exists(released) and not os.path.exists(orphan)
