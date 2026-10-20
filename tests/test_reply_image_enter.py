"""An Enter right after a pasted image path waits for its chip, so Claude Code takes it."""

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


CHIP_DELAY = 0.3  # how long the fake Claude Code "pastes" before the chip shows


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
    sess = cr.SESSIONS[998]
    log: list[tuple[float, bytes]] = []
    images = 0

    def chip(n):
        # Like Claude Code's redraw: a cursor move stands in for the space.
        for q in list(sess.subscribers):
            q.put_nowait(("output", f"\x1b[2m[Image\x1b[1C#{n}]\x1b[0m".encode()))

    async def record(_sess, data):
        nonlocal images
        log.append((time.monotonic(), data))
        if data.startswith(b"\x1b[200~") and data.rstrip(b" \x1b[201~").endswith(b".png"):
            images += 1
            asyncio.get_running_loop().call_later(CHIP_DELAY, chip, images)

    monkeypatch.setattr(cr, "_write_pty", record)
    monkeypatch.setattr(cr, "PASTE_ATTACH_TIMEOUT_SECONDS", 1.0)
    yield log
    cr.SESSIONS.pop(998, None)
    proc.kill()
    proc.wait()
    os.close(master)


def _run(msgs):
    asyncio.run(cr.serve(FakeWS([{"type": "attach"}, *msgs]), 998))


def _image(name="image.png"):
    return {"type": "file", "name": name, "data": base64.b64encode(PNG).decode()}


def test_enter_after_image_waits_for_the_chip(written):
    _run([
        _image(),
        {"type": "input", "data": "what is this?"},
        {"type": "input", "data": "\r"},
    ])
    (t_paste, paste), (_, text), (t_enter, enter) = written
    path = shlex.split(paste.decode().removeprefix("\x1b[200~").removesuffix("\x1b[201~"))[0]
    saved = Path(path)
    assert saved.read_bytes() == PNG
    saved.unlink()
    assert (text, enter) == (b"what is this?", b"\r")
    assert CHIP_DELAY <= t_enter - t_paste < 1.0


def test_enter_waits_for_every_image(written):
    _run([_image(), _image(), {"type": "input", "data": "\r"}])
    (t_paste, _), _, (t_enter, enter) = written
    assert enter == b"\r"
    assert CHIP_DELAY <= t_enter - t_paste < 1.0


def test_enter_gives_up_after_the_timeout(written, monkeypatch):
    monkeypatch.setattr(cr, "_image_numbers", lambda data: [])
    _run([_image(), {"type": "input", "data": "\r"}])
    (t_paste, _), (t_enter, enter) = written
    assert enter == b"\r"
    assert t_enter - t_paste >= 1.0


def test_enter_after_other_file_goes_straight_in(written):
    _run([_image("notes.txt"), {"type": "input", "data": "\r"}])
    (t_paste, _), (t_enter, _) = written
    assert t_enter - t_paste < 0.2


def test_enter_without_image_goes_straight_in(written):
    start = time.monotonic()
    _run([{"type": "input", "data": "hi"}, {"type": "input", "data": "\r"}])
    assert [d for _, d in written] == [b"hi", b"\r"]
    assert written[-1][0] - start < 0.2
