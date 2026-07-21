"""Tests for the warm connection pool's caching and keepalive gating."""

from __future__ import annotations

import threading
import time
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

        def execute_stateful_command(self, command: str, timeout_seconds: float | None = None) -> object:
            raise AssertionError("should not touch a local host")

    cp._ping_host(cast(Any, SimpleNamespace()), cast(Any, _LocalHost()))  # must not raise


def test_ping_host_touches_remote_with_timeout() -> None:
    seen: list[tuple[str, float | None]] = []

    class _RemoteHost:
        is_local = False

        def execute_stateful_command(self, command: str, timeout_seconds: float | None = None) -> object:
            seen.append((command, timeout_seconds))
            return SimpleNamespace(success=True)

    cp._ping_host(cast(Any, SimpleNamespace()), cast(Any, _RemoteHost()))
    # The keepalive touch is bounded so a hung host can't wedge the connection.
    assert seen == [("true", cp._KEEPALIVE_TIMEOUT_SECONDS)]


class _RecordingHost:
    """Fake host whose command records concurrency and can sleep to simulate lag."""

    is_local = False

    def __init__(self, name: str, delay: float, tracker: dict[str, Any]) -> None:
        self._name = name
        self._delay = delay
        self._tracker = tracker

    def execute_stateful_command(self, command: str, timeout_seconds: float | None = None) -> object:
        with self._tracker["lock"]:
            self._tracker["live"] += 1
            self._tracker["max_live"] = max(self._tracker["max_live"], self._tracker["live"])
            self._tracker["pinged"].add(self._name)
        time.sleep(self._delay)
        with self._tracker["lock"]:
            self._tracker["live"] -= 1
        return SimpleNamespace(success=True)


def _seed_handle(pool: ConnectionPool, name: str, host: object) -> None:
    handle = pool._handle_for(name)
    handle.agent = cast(Any, SimpleNamespace())
    handle.host = cast(Any, host)
    handle.matches = []
    handle.matches_at = time.monotonic()  # keep get_send_matches from re-resolving


def test_run_on_host_serializes_two_agents_on_one_host() -> None:
    # Two agents resolve to the SAME host object (as lima/modal/vps providers
    # return); the per-host lock must stop them driving it concurrently.
    tracker: dict[str, Any] = {"lock": threading.Lock(), "live": 0, "max_live": 0, "pinged": set()}
    shared = _RecordingHost("shared", 0.05, tracker)
    pool = _pool()
    _seed_handle(pool, "a", shared)
    _seed_handle(pool, "b", shared)

    threads = [threading.Thread(target=lambda n=n: pool.run_on_host(n, cp._ping_host)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tracker["max_live"] == 1  # never concurrent on the shared connection


def test_run_on_host_allows_distinct_hosts_concurrently() -> None:
    # Two agents on two different host objects should NOT serialize against each
    # other (the lock is per-host, not global).
    tracker: dict[str, Any] = {"lock": threading.Lock(), "live": 0, "max_live": 0, "pinged": set()}
    pool = _pool()
    _seed_handle(pool, "a", _RecordingHost("a", 0.1, tracker))
    _seed_handle(pool, "b", _RecordingHost("b", 0.1, tracker))

    threads = [threading.Thread(target=lambda n=n: pool.run_on_host(n, cp._ping_host)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tracker["max_live"] == 2  # ran in parallel on their separate hosts


def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_maintainer_registers_wake_and_warms_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    # The maintainer must register its wake with the registry and warm "on" agents
    # right away (immediate first tick), not after a full keepalive interval.
    monkeypatch.setattr(cp, "_KEEPALIVE_INTERVAL_SECONDS", 100.0)  # so any tick is immediate/wake-driven
    tracker: dict[str, Any] = {"lock": threading.Lock(), "live": 0, "max_live": 0, "pinged": set()}
    captured: list[Any] = []
    registry = cast(
        Any,
        SimpleNamespace(
            snapshot=lambda: [{"name": "a", "state": "RUNNING"}],
            set_on_agents_changed=lambda cb: captured.append(cb),
        ),
    )
    pool = _pool()
    _seed_handle(pool, "a", _RecordingHost("a", 0.0, tracker))
    pool.start_maintainer(registry)
    try:
        assert _wait_until(lambda: "a" in tracker["pinged"])  # warmed without the 100s wait
        assert captured == [pool._wake.set]  # registered its wake callback
        # A change notification triggers another tick promptly.
        with tracker["lock"]:
            tracker["pinged"].clear()
        captured[0]()  # simulate the registry firing on a new agent
        assert _wait_until(lambda: "a" in tracker["pinged"])
    finally:
        pool.stop()


def test_tick_warms_all_hosts_concurrently_despite_lag() -> None:
    # A slow host must not serialize the keepalive: two ~0.4s hosts warmed in
    # parallel finish in ~0.4s, not ~0.8s, and both get pinged.
    tracker: dict[str, Any] = {"lock": threading.Lock(), "live": 0, "max_live": 0, "pinged": set()}
    pool = _pool()
    _seed_handle(pool, "a", _RecordingHost("a", 0.4, tracker))
    _seed_handle(pool, "b", _RecordingHost("b", 0.4, tracker))
    pool._registry = cast(
        Any,
        SimpleNamespace(snapshot=lambda: [{"name": "a", "state": "RUNNING"}, {"name": "b", "state": "RUNNING"}]),
    )

    start = time.monotonic()
    pool._tick()
    elapsed = time.monotonic() - start
    assert tracker["pinged"] == {"a", "b"}  # loop reached every host
    assert elapsed < 0.7  # concurrent (serial would be ~0.8s)
