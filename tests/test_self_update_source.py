"""``self-update`` for ``uv tool`` installs built from a local checkout."""

from __future__ import annotations

import os

import pytest

from ntasker import service

UV_PYTHON = "/home/u/.local/share/uv/tools/ntasker/bin/python"


def _receipt(tmp_path, requirement: str) -> str:
    (tmp_path / "uv-receipt.toml").write_text(
        f"[tool]\nrequirements = [{requirement}]\n", encoding="utf-8"
    )
    return str(tmp_path)


@pytest.fixture
def uv_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(service.sys, "executable", UV_PYTHON)
    monkeypatch.setattr(service.sys, "prefix", str(tmp_path))
    return tmp_path


def test_registry_install_upgrades_in_place(uv_tool):
    _receipt(uv_tool, '{ name = "ntasker", extras = ["voice"] }')
    assert service.resolve_update_command(None) == ["uv", "tool", "upgrade", "ntasker"]
    assert service.local_install_source() is None


def test_directory_install_switches_to_pypi(uv_tool):
    _receipt(uv_tool, '{ name = "ntasker", directory = "/Users/x/ntasker-ui" }')
    assert service.resolve_update_command(None) == ["uv", "tool", "install", "--upgrade", "ntasker"]
    assert service.local_install_source() == "/Users/x/ntasker-ui"


def test_directory_install_keeps_extras(uv_tool):
    _receipt(uv_tool, '{ name = "ntasker", extras = ["voice"], editable = "/src/ntasker" }')
    assert service.resolve_update_command(None) == [
        "uv", "tool", "install", "--upgrade", "ntasker[voice]",
    ]


def test_missing_receipt_falls_back_to_upgrade(uv_tool):
    assert service.resolve_update_command(None) == ["uv", "tool", "upgrade", "ntasker"]
    assert service.local_install_source() is None


def test_setting_still_wins(uv_tool):
    _receipt(uv_tool, '{ name = "ntasker", directory = "/Users/x/ntasker-ui" }')
    assert service.resolve_update_command("uv tool upgrade ntasker") == [
        "uv", "tool", "upgrade", "ntasker",
    ]


def test_cli_names_the_local_source(uv_tool, monkeypatch, capsys, tmp_path):
    import subprocess
    import types

    from ntasker.cli import main

    _receipt(uv_tool, '{ name = "ntasker", directory = "/Users/x/ntasker-ui" }')
    ran = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0, stdout="ntasker 9.9.9"),
    )
    monkeypatch.setattr(service, "restart_service", lambda: False)
    assert main(["--db", str(tmp_path / "none.db"), "self-update"]) == 0
    assert os.path.basename(ran[0][0]) == "uv"
    assert ran[0][1:] == ["tool", "install", "--upgrade", "ntasker"]
    assert "/Users/x/ntasker-ui" in capsys.readouterr().out
