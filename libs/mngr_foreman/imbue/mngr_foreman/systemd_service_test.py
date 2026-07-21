"""Tests for the foreman systemd install/uninstall helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from imbue.mngr_foreman import systemd_service as ss

# --- pure builders ---


def test_render_unit_file_has_all_required_directives() -> None:
    text = ss.render_unit_file(
        user="ubuntu",
        exec_start="/opt/venv/bin/mngr foreman --host 0.0.0.0 --port 8700",
        working_dir="/home/ubuntu/mngr",
        path_env="/opt/venv/bin:/usr/bin",
    )
    for needle in (
        "Type=simple",
        "User=ubuntu",
        "WorkingDirectory=/home/ubuntu/mngr",
        "Environment=PATH=/opt/venv/bin:/usr/bin",
        "ExecStart=/opt/venv/bin/mngr foreman --host 0.0.0.0 --port 8700",
        "Restart=always",
        "RestartSec=3",
        "After=network.target docker.service",
        "WantedBy=multi-user.target",
    ):
        assert needle in text


def test_build_exec_start() -> None:
    assert (
        ss.build_exec_start("/opt/venv/bin/mngr", "0.0.0.0", 8700)
        == "/opt/venv/bin/mngr foreman --host 0.0.0.0 --port 8700"
    )


def test_default_working_dir_from_venv_checkout() -> None:
    assert ss.default_working_dir("/home/u/mngr/.venv/bin/mngr") == "/home/u/mngr"


def test_default_working_dir_falls_back_to_home() -> None:
    # A binary not under a .venv gives no checkout to point at -> $HOME.
    assert ss.default_working_dir("/usr/local/bin/mngr") == str(Path.home())


def test_default_path_env_leads_with_bindir_has_system_dirs_and_dedups() -> None:
    path = ss.default_path_env("/opt/venv/bin/mngr")
    parts = path.split(":")
    assert parts[0] == "/opt/venv/bin"
    assert "/usr/bin" in parts and "/usr/local/bin" in parts
    assert len(parts) == len(set(parts))  # no duplicates


# --- binary resolution ---


def test_resolve_abs_mngr_binary_returns_existing_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "mngr"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ss, "resolve_mngr_binary", lambda: str(fake))
    assert ss.resolve_abs_mngr_binary() == str(fake)


def test_resolve_abs_mngr_binary_raises_on_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "resolve_mngr_binary", lambda: "mngr")  # bare fallback
    monkeypatch.setattr(ss.shutil, "which", lambda _name: None)
    with pytest.raises(ss.ServiceInstallError):
        ss.resolve_abs_mngr_binary()


# --- privilege prefix + command runner ---


def test_privileged_prefix_root_vs_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss.os, "geteuid", lambda: 0)
    assert ss._privileged_prefix() == []
    monkeypatch.setattr(ss.os, "geteuid", lambda: 1000)
    assert ss._privileged_prefix() == ["sudo"]


def test_run_privileged_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        ss.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stderr="nope", stdout="")
    )
    with pytest.raises(ss.ServiceInstallError):
        ss._run_privileged(["systemctl", "daemon-reload"])


def test_run_privileged_ok_on_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ss.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stderr="", stdout=""))
    ss._run_privileged(["systemctl", "daemon-reload"])  # must not raise


# --- install / uninstall orchestration ---


def test_install_service_writes_unit_then_reloads_and_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ss, "resolve_abs_mngr_binary", lambda: "/opt/venv/bin/mngr")
    monkeypatch.setattr(ss.getpass, "getuser", lambda: "ubuntu")
    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(ss, "_run_privileged", lambda argv, input_text=None: calls.append((argv, input_text)))

    unit_text = ss.install_service("0.0.0.0", 8700)

    # 1) tee the unit to the unit path, with the rendered text on stdin.
    assert calls[0][0] == ["tee", str(ss.UNIT_PATH)]
    assert calls[0][1] == unit_text
    assert "ExecStart=/opt/venv/bin/mngr foreman --host 0.0.0.0 --port 8700" in unit_text
    # 2) + 3) reload + enable --now, in order.
    argvs = [c[0] for c in calls]
    assert ["systemctl", "daemon-reload"] in argvs
    assert ["systemctl", "enable", "--now", "foreman"] in argvs
    assert argvs.index(["systemctl", "enable", "--now", "foreman"]) == len(argvs) - 1


def test_uninstall_service_absent_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ss, "UNIT_PATH", tmp_path / "foreman.service")  # does not exist
    calls: list[list[str]] = []
    monkeypatch.setattr(ss, "_run_privileged", lambda argv, input_text=None: calls.append(argv))
    assert ss.uninstall_service() is False
    assert calls == []


def test_uninstall_service_present_disables_and_removes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unit = tmp_path / "foreman.service"
    unit.write_text("[Service]\n")
    monkeypatch.setattr(ss, "UNIT_PATH", unit)
    calls: list[list[str]] = []
    monkeypatch.setattr(ss, "_run_privileged", lambda argv, input_text=None: calls.append(argv))
    assert ss.uninstall_service() is True
    assert ["systemctl", "disable", "--now", "foreman"] in calls
    assert ["rm", "-f", str(unit)] in calls
    assert ["systemctl", "daemon-reload"] in calls
