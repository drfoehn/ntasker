"""Finding ``uv`` when the service's ``PATH`` lacks ``~/.local/bin`` (#261)."""

from __future__ import annotations

import os

import pytest

from ntasker import service


@pytest.fixture
def no_uv_on_path(monkeypatch, tmp_path):
    """launchd-like environment: minimal PATH, empty home."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    monkeypatch.setattr(service, "_uv_candidates", lambda: [str(tmp_path / ".local" / "bin" / "uv")])
    return tmp_path


def _make_uv(path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_finds_uv_outside_path(no_uv_on_path):
    uv = _make_uv(no_uv_on_path / ".local" / "bin" / "uv")
    assert service.find_uv() == uv
    assert service.runnable(["uv", "tool", "upgrade", "ntasker"]) == [uv, "tool", "upgrade", "ntasker"]


def test_no_uv_anywhere_keeps_command(no_uv_on_path):
    assert service.find_uv() is None
    assert service.runnable(["uv", "tool", "upgrade", "ntasker"]) == ["uv", "tool", "upgrade", "ntasker"]


def test_other_commands_untouched(no_uv_on_path):
    _make_uv(no_uv_on_path / ".local" / "bin" / "uv")
    cmd = ["/usr/bin/python3", "-m", "pip", "install", "-U", "ntasker"]
    assert service.runnable(cmd) == cmd


def test_candidates_cover_installer_locations(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UV", "/x/uv")
    cands = service._uv_candidates()
    assert cands[0] == "/x/uv"
    assert os.path.join(str(tmp_path), ".local", "bin", "uv") in cands
