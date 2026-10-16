"""``self-update`` for an editable install (a development checkout)."""

from __future__ import annotations

import json
import types

from ntasker import service

VENV_PYTHON = "/home/u/ntasker/.venv/bin/python"


def _dist(direct_url: dict | None):
    text = json.dumps(direct_url) if direct_url is not None else None
    return types.SimpleNamespace(read_text=lambda name: text)


def test_editable_source_reads_direct_url(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "distribution",
        lambda name: _dist({"url": "file:///src/ntasker", "dir_info": {"editable": True}}),
    )
    assert service.editable_source() == "/src/ntasker"


def test_regular_install_has_no_editable_source(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: _dist(None))
    assert service.editable_source() is None
    monkeypatch.setattr(
        importlib.metadata, "distribution",
        lambda name: _dist({"url": "file:///src/ntasker", "dir_info": {}}),
    )
    assert service.editable_source() is None


def test_editable_install_reinstalls_from_checkout(monkeypatch):
    monkeypatch.setattr(service.sys, "executable", VENV_PYTHON)
    monkeypatch.setattr(service, "editable_source", lambda: "/src/ntasker")
    monkeypatch.setattr(service, "_has_pip", lambda: False)
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/uv")
    assert service.resolve_update_command(None) == [
        "uv", "pip", "install", "--python", VENV_PYTHON, "-e", "/src/ntasker",
    ]


def test_regular_venv_upgrades_from_pypi(monkeypatch):
    monkeypatch.setattr(service.sys, "executable", VENV_PYTHON)
    monkeypatch.setattr(service, "editable_source", lambda: None)
    monkeypatch.setattr(service, "_has_pip", lambda: True)
    assert service.resolve_update_command(None) == [VENV_PYTHON, "-m", "pip", "install", "-U", "ntasker"]


def test_cli_names_the_checkout(monkeypatch, capsys, tmp_path):
    import subprocess

    from ntasker.cli import main

    monkeypatch.setattr(service.sys, "executable", VENV_PYTHON)
    monkeypatch.setattr(service, "editable_source", lambda: "/src/ntasker")
    monkeypatch.setattr(service, "_has_pip", lambda: True)
    ran = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0, stdout="ntasker 9.9.9"),
    )
    monkeypatch.setattr(service, "restart_service", lambda: False)
    assert main(["--db", str(tmp_path / "none.db"), "self-update"]) == 0
    assert ran[0] == [VENV_PYTHON, "-m", "pip", "install", "-e", "/src/ntasker"]
    assert "/src/ntasker" in capsys.readouterr().out
