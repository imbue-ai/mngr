"""Tests for the terminal helpers -- the ControlMaster keepalive pre-warm."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mngr_foreman import terminal as term
from imbue.mngr_foreman.connection_pool import ConnectionPool


def _pool_running_on(host: object) -> ConnectionPool:
    """A stand-in pool whose run_on_host just applies the fn to (agent, host)."""
    return cast(ConnectionPool, SimpleNamespace(run_on_host=lambda _name, fn: fn(SimpleNamespace(), host)))


def test_prewarm_skips_local_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # A local host has no ssh, so the pre-warm must never spawn a subprocess.
    ran: list[Any] = []
    monkeypatch.setattr(term.subprocess, "run", lambda *a, **k: ran.append(a))
    term.prewarm_agent_control_master(_pool_running_on(SimpleNamespace(is_local=True)), "a")
    assert ran == []


def test_prewarm_spawns_ssh_with_control_master_for_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    # A remote host: the pre-warm spawns `ssh <base> <control-master opts> true`,
    # opening the same multiplexing master the terminal reuses.
    calls: list[list[str]] = []
    monkeypatch.setattr(term, "build_ssh_base_args", lambda host: ["ssh", "user@remote"])
    monkeypatch.setattr(term, "_control_master_opts", lambda: ["-o", "ControlMaster=auto"])
    monkeypatch.setattr(term.subprocess, "run", lambda argv, **_k: calls.append(argv))
    term.prewarm_agent_control_master(_pool_running_on(SimpleNamespace(is_local=False)), "a")
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "ssh"
    assert "ControlMaster=auto" in argv
    assert argv[-1] == "true"  # cheapest remote command; opens the master socket


def test_prewarm_swallows_build_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # If resolution fails, the pre-warm is best-effort: it must not raise (a terminal
    # open would warm the master itself).
    def _boom(_name: str, _fn: object) -> None:
        raise RuntimeError("host unreachable")

    pool = cast(ConnectionPool, SimpleNamespace(run_on_host=_boom))
    ran: list[Any] = []
    monkeypatch.setattr(term.subprocess, "run", lambda *a, **k: ran.append(a))
    term.prewarm_agent_control_master(pool, "a")  # must not raise
    assert ran == []
