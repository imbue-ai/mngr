"""Tests for the warm connection pool's caching and keepalive gating."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_foreman import connection_pool as cp
from imbue.mngr_foreman.connection_pool import ConnectionPool


def _pool() -> ConnectionPool:
    return ConnectionPool(cast(MngrContext, SimpleNamespace()))


def test_send_matches_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(cp, "parse_agent_address", lambda name: name)

    def _find_all(**_kw: object) -> list[str]:
        calls["n"] += 1
        return ["match"]

    monkeypatch.setattr(cp, "find_all_agents", _find_all)
    pool = _pool()
    assert pool.get_send_matches("a") == ["match"]
    assert pool.get_send_matches("a") == ["match"]
    assert calls["n"] == 1  # second call served from cache


def test_send_matches_ttl_reresolves(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(cp, "parse_agent_address", lambda name: name)
    monkeypatch.setattr(cp, "find_all_agents", lambda **_kw: [calls.__setitem__("n", calls["n"] + 1) or "m"])
    monkeypatch.setattr(cp, "_MATCHES_TTL_SECONDS", 0.0)  # expire immediately
    pool = _pool()
    pool.get_send_matches("a")
    pool.get_send_matches("a")
    assert calls["n"] == 2  # TTL forced a re-resolve


def test_run_on_host_caches_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"r": 0}
    monkeypatch.setattr(cp, "parse_agent_address", lambda name: name)
    monkeypatch.setattr(cp, "find_one_agent", lambda addr, ctx: ("hr", "ar"))

    def _resolve(**_kw: object) -> tuple[object, object]:
        calls["r"] += 1
        return SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(cp, "resolve_to_started_host_and_agent", _resolve)
    pool = _pool()
    assert pool.run_on_host("a", lambda _ag, _h: 1) == 1
    assert pool.run_on_host("a", lambda _ag, _h: 2) == 2
    assert calls["r"] == 1  # resolved once, reused


def test_run_on_host_invalidates_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"r": 0}
    monkeypatch.setattr(cp, "parse_agent_address", lambda name: name)
    monkeypatch.setattr(cp, "find_one_agent", lambda addr, ctx: ("hr", "ar"))

    def _resolve(**_kw: object) -> tuple[object, object]:
        calls["r"] += 1
        return SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(cp, "resolve_to_started_host_and_agent", _resolve)
    pool = _pool()

    def _boom(_ag: object, _h: object) -> None:
        raise RuntimeError("host died")

    with pytest.raises(RuntimeError):
        pool.run_on_host("a", _boom)
    # after the failure the handle was dropped, so a good call re-resolves
    pool.run_on_host("a", lambda _ag, _h: None)
    assert calls["r"] == 2


def test_ping_host_skips_local() -> None:
    class _LocalHost:
        is_local = True

        def execute_stateful_command(self, command: str) -> object:
            raise AssertionError("should not touch a local host")

    cp._ping_host(cast(Any, SimpleNamespace()), cast(Any, _LocalHost()))  # must not raise


def test_ping_host_touches_remote() -> None:
    seen: list[str] = []

    class _RemoteHost:
        is_local = False

        def execute_stateful_command(self, command: str) -> object:
            seen.append(command)
            return SimpleNamespace(success=True)

    cp._ping_host(cast(Any, SimpleNamespace()), cast(Any, _RemoteHost()))
    assert seen == ["true"]
