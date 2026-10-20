"""An Enter right after a pasted image path waits, so Claude Code takes it."""

import asyncio
import base64
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect

from ntasker import claude_runner as cr
from ntasker.db import init_db, set_db_path

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeWS:
    """Plays a fixed client message sequence, then disconnects."""

    def __init__(self, msgs):
        self.msgs = list(msgs)

    async def receive_json(self):
        if not self.msgs:
            raise WebSocketDisconnect()
        return self.msgs.pop(0)

    async def send_json(self, data):
        pass


@pytest.fixture
def written(tmp_path, monkeypatch):
    set_db_path(tmp_path / "t.db")
    init_db(tmp_path / "t.db")
    master, slave = os.openpty()
    proc = subprocess.Popen(
        ["bash", "-c", "sleep 986"], stdin=slave, stdout=slave, stderr=slave,
        preexec_fn=cr._child_setup, close_fds=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    cr.SESSIONS[998] = cr.TermSession(task_id=998, proc=proc, master_fd=master)
    log: list[tuple[float, bytes]] = []

    async def record(_sess, data):
        log.append((time.monotonic(), data))

    monkeypatch.setattr(cr, "_write_pty", record)
    monkeypatch.setattr(cr, "PASTE_SUBMIT_GAP_SECONDS", 0.3)
    yield log
    cr.SESSIONS.pop(998, None)
    proc.kill()
    proc.wait()
    os.close(master)


def _run(msgs):
    asyncio.run(cr.serve(FakeWS([{"type": "attach"}, *msgs]), 998))


def test_enter_after_image_waits_for_the_gap(written):
    _run([
        {"type": "file", "name": "image.png", "data": base64.b64encode(PNG).decode()},
        {"type": "input", "data": "what is this?"},
        {"type": "input", "data": "\r"},
    ])
    (t_paste, paste), (_, text), (t_enter, enter) = written
    path = shlex.split(paste.decode().removeprefix("\x1b[200~").removesuffix("\x1b[201~"))[0]
    saved = Path(path)
    assert saved.read_bytes() == PNG
    saved.unlink()
    assert (text, enter) == (b"what is this?", b"\r")
    assert t_enter - t_paste >= 0.3


def test_enter_without_image_goes_straight_in(written):
    start = time.monotonic()
    _run([{"type": "input", "data": "hi"}, {"type": "input", "data": "\r"}])
    assert [d for _, d in written] == [b"hi", b"\r"]
    assert written[-1][0] - start < 0.2
