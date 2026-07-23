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
from imbue.mngr_foreman.connection_pool import _Handle


def _pool() -> ConnectionPool:
    return ConnectionPool(cast(MngrContext, SimpleNamespace()))


def test_drop_handles_disconnects_only_unshared_hosts() -> None:
    # Dropping a handle must CLOSE its SSH connection (so the paramiko reader thread
    # exits -- the leak fix) but NEVER close a host object a surviving handle shares.
    class _Host:
        def __init__(self) -> None:
            self.closed = 0

        def disconnect(self) -> None:
            self.closed += 1

    shared = _Host()  # used by two agents on the same host
    solo = _Host()  # used by one
    pool = _pool()
    pool._handles = {
        "a": _Handle(host=cast(Any, shared)),
        "b": _Handle(host=cast(Any, shared)),
        "c": _Handle(host=cast(Any, solo)),
    }
    pool._drop_handles({"a", "c"})
    assert set(pool._handles) == {"b"}  # a, c removed; b survives
    assert solo.closed == 1  # unshared -> connection closed
    assert shared.closed == 0  # still referenced by b -> left intact
    pool._drop_handles({"b"})  # last referrer of shared now leaves
    assert shared.closed == 1  # -> now closed


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


def test_send_via_pool_never_auto_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Foreman must never resurrect a stopped agent: the send passes is_start_desired=False.
    captured: dict[str, Any] = {}

    def _send(**kw: Any) -> object:
        captured.update(kw)
        return SimpleNamespace(failed_agents=[])

    monkeypatch.setattr("imbue.mngr.api.message.send_message_to_agents", _send)
    pool = _pool()
    handle = pool._handle_for("a")
    handle.matches = ["m"]  # seed cached matches so no resolution runs
    handle.matches_at = time.monotonic()
    assert cp.send_via_pool(pool, "a", "hello") == []
    assert captured["is_start_desired"] is False


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


def test_run_on_host_keeps_handle_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient command failure must NOT tear down the cached connection: the
    # handle is reused (no re-resolve) and the error just propagates. Reconnection
    # is the keepalive's job alone.
    calls = {"r": 0}
    monkeypatch.setattr(cp, "parse_agent_address", lambda name: name)
    monkeypatch.setattr(cp, "find_one_agent", lambda addr, ctx: ("hr", "ar"))

    def _resolve(**_kw: object) -> tuple[object, object]:
        calls["r"] += 1
        return SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(cp, "resolve_to_started_host_and_agent", _resolve)
    pool = _pool()

    def _boom(_ag: object, _h: object) -> None:
        raise RuntimeError("transient hiccup")

    with pytest.raises(RuntimeError):
        pool.run_on_host("a", _boom)
    # The handle survived, so the next call reuses the cached resolution.
    pool.run_on_host("a", lambda _ag, _h: None)
    assert calls["r"] == 1


def test_warm_one_no_reconnect_on_touch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # The keepalive no longer pings/reconnects the paramiko connection (that churn was
    # the leak). A failed _touch just logs and moves on -- it must NOT re-touch/reconnect.
    monkeypatch.setattr(cp, "parse_agent_address", lambda name: name)
    calls = {"touch": 0}

    def _boom_matches(**_kw: object) -> list[str]:
        calls["touch"] += 1
        raise RuntimeError("discovery hiccup")

    monkeypatch.setattr(cp, "find_all_agents", _boom_matches)
    pool = _pool()
    pool._warm_one("a")  # must not raise, must not retry
    assert calls["touch"] == 1  # exactly one attempt -- no reconnect loop


def _touch_host(_agent: Any, host: Any) -> None:
    """A trivial run_on_host fn used to exercise the per-host lock in the tests below."""
    host.execute_stateful_command("true")


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

    threads = [threading.Thread(target=lambda n=n: pool.run_on_host(n, _touch_host)) for n in ("a", "b")]
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

    threads = [threading.Thread(target=lambda n=n: pool.run_on_host(n, _touch_host)) for n in ("a", "b")]
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


def _track_prewarm(monkeypatch: pytest.MonkeyPatch, warmed: set[str], delay: float = 0.0) -> None:
    """Patch out the two things _touch does (refresh matches + prewarm ControlMaster)
    and record which agents got warmed via the ControlMaster prewarm. No paramiko."""
    monkeypatch.setattr(cp, "parse_agent_address", lambda name: name)
    monkeypatch.setattr(cp, "find_all_agents", lambda **_kw: [])  # get_send_matches -> no discovery
    lock = threading.Lock()

    def _fake_prewarm(_pool: Any, name: str) -> None:
        if delay:
            time.sleep(delay)
        with lock:
            warmed.add(name)

    monkeypatch.setattr("imbue.mngr_foreman.terminal.prewarm_agent_control_master", _fake_prewarm)


def test_maintainer_registers_wake_and_warms_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    # The maintainer registers its wake with the registry and warms "on" agents right
    # away -- warming = refresh send matches + prewarm the ControlMaster socket (NO
    # paramiko ping; that ping was the leak).
    monkeypatch.setattr(cp, "_KEEPALIVE_INTERVAL_SECONDS", 100.0)  # so any tick is immediate/wake-driven
    warmed: set[str] = set()
    _track_prewarm(monkeypatch, warmed)
    captured: list[Any] = []
    registry = cast(
        Any,
        SimpleNamespace(
            snapshot=lambda: [{"name": "a", "state": "RUNNING"}],
            set_on_change=lambda cb: captured.append(cb),
        ),
    )
    pool = _pool()
    pool.start_maintainer(registry)
    try:
        assert _wait_until(lambda: "a" in warmed)  # warmed without the 100s wait
        assert captured == [pool._wake.set]  # registered its wake callback
        warmed.clear()
        captured[0]()  # simulate the registry firing on a new agent
        assert _wait_until(lambda: "a" in warmed)
    finally:
        pool.stop()


def test_tick_warms_all_agents_concurrently_despite_lag(monkeypatch: pytest.MonkeyPatch) -> None:
    # A slow host must not serialize the maintainer: two ~0.4s prewarms run in parallel
    # (~0.4s, not ~0.8s), and both agents get warmed.
    warmed: set[str] = set()
    _track_prewarm(monkeypatch, warmed, delay=0.4)
    pool = _pool()
    pool._registry = cast(
        Any,
        SimpleNamespace(snapshot=lambda: [{"name": "a", "state": "RUNNING"}, {"name": "b", "state": "RUNNING"}]),
    )
    start = time.monotonic()
    pool._tick()
    elapsed = time.monotonic() - start
    assert warmed == {"a", "b"}  # loop reached every agent
    assert elapsed < 0.7  # concurrent (serial would be ~0.8s)
