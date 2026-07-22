"""Tests for the discovery-poll agent registry and its live-coding filter."""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_foreman import agent_registry as ar
from imbue.mngr_foreman.agent_registry import AgentRegistry


def _fake_agent(agent_id: str, name: str, state: str = "WAITING", agent_type: str = "claude") -> AgentDetails:
    """Minimal stand-in exposing the fields the registry filter + projection read."""
    return cast(
        AgentDetails,
        SimpleNamespace(
            id=agent_id,
            name=name,
            type=agent_type,
            state=state,
            host=SimpleNamespace(name="boxa", provider_name="ssh"),
            labels={},
            agent_activity_time=None,
            user_activity_time=None,
            create_time=None,
        ),
    )


def _registry() -> AgentRegistry:
    return AgentRegistry(cast(MngrContext, SimpleNamespace()))


def _patch_list(monkeypatch: pytest.MonkeyPatch, agents: tuple[AgentDetails, ...]) -> None:
    monkeypatch.setattr(ar, "list_agents", lambda *_a, **_k: SimpleNamespace(agents=agents))


def _patch_providers(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...] = ("p1",)) -> None:
    monkeypatch.setattr(ar, "get_all_provider_instances", lambda *_a, **_k: [SimpleNamespace(name=n) for n in names])


def test_poll_keeps_only_live_coding_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    # A running claude and a waiting codex are shown; a stopped claude, a done
    # opencode, and a running non-coding worker are all hidden.
    _patch_list(
        monkeypatch,
        (
            _fake_agent("a1", "alpha", state="RUNNING", agent_type="claude"),
            _fake_agent("a2", "beta", state="WAITING", agent_type="codex"),
            _fake_agent("a3", "gamma", state="STOPPED", agent_type="claude"),
            _fake_agent("a4", "delta", state="DONE", agent_type="opencode"),
            _fake_agent("a5", "worker", state="RUNNING", agent_type="mngr_worker"),
        ),
    )
    registry = _registry()
    registry._poll_provider("p1")
    assert {c["name"] for c in registry.snapshot()} == {"alpha", "beta"}


@pytest.mark.parametrize("agent_type", ["claude", "codex", "opencode", "pi-coding"])
def test_each_coding_type_is_shown(monkeypatch: pytest.MonkeyPatch, agent_type: str) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING", agent_type=agent_type),))
    registry = _registry()
    registry._poll_provider("p1")
    assert [c["name"] for c in registry.snapshot()] == ["alpha"]


@pytest.mark.parametrize("state", ["STOPPED", "DONE", "REPLACED", "UNKNOWN", "RUNNING_UNKNOWN_AGENT_TYPE"])
def test_non_live_states_are_hidden(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state=state, agent_type="claude"),))
    registry = _registry()
    registry._poll_provider("p1")
    assert registry.snapshot() == []


def test_get_agent_by_name_and_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    registry._poll_provider("p1")
    assert registry.get_agent("alpha") is not None
    assert registry.get_agent("a1") is not None
    assert registry.get_agent("nope") is None


def test_poll_broadcasts_snapshot_only_on_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    q: queue.Queue[dict] = queue.Queue()
    registry._subscribers.add(q)
    registry._poll_provider("p1")
    registry._poll_provider("p1")  # identical result -> must not re-broadcast
    assert q.qsize() == 1
    msg = q.get_nowait()
    assert msg["type"] == "snapshot"
    assert [a["name"] for a in msg["agents"]] == ["alpha"]


def test_on_change_fires_only_when_name_set_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    fired: list[int] = []
    registry = _registry()
    registry.set_on_change(lambda: fired.append(1))

    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry._poll_provider("p1")
    registry._poll_provider("p1")  # same name set -> no extra fire
    assert len(fired) == 1

    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"), _fake_agent("a2", "beta", state="WAITING")))
    registry._poll_provider("p1")  # beta appeared -> fire so the pool warms it
    assert len(fired) == 2

    _patch_list(monkeypatch, ())
    registry._poll_provider("p1")  # both gone -> fire so the pool drops them
    assert len(fired) == 3


def test_poll_keeps_last_set_on_list_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    registry._poll_provider("p1")

    def _boom(*_a: object, **_k: object) -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setattr(ar, "list_agents", _boom)
    registry._poll_provider("p1")  # must not raise; keeps the previous set
    assert [c["name"] for c in registry.snapshot()] == ["alpha"]


def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, ())
    _patch_providers(monkeypatch, ())  # no providers -> the loop does nothing
    registry = _registry()
    registry.start()
    first = registry._thread
    registry.start()
    try:
        assert registry._thread is first  # one poll thread, not two
    finally:
        registry.stop()


def test_hung_provider_does_not_freeze_others(monkeypatch: pytest.MonkeyPatch) -> None:
    # The core robustness fix: one provider whose discovery blocks must NOT stop
    # another provider's agents from being published. "fast" returns immediately;
    # "slow" blocks forever -- fast's agent must still appear.
    started = threading.Event()

    def _list(*_a: object, **kwargs: object) -> Any:
        names = kwargs.get("provider_names") or ()
        if "slow" in names:
            started.set()
            time.sleep(30)  # simulate a hung provider (never returns during the test)
        return SimpleNamespace(agents=(_fake_agent("f1", "fast", state="RUNNING"),) if "fast" in names else ())

    monkeypatch.setattr(ar, "list_agents", _list)
    _patch_providers(monkeypatch, ("fast", "slow"))
    registry = _registry()
    registry._poll_once()  # fans out one thread per provider
    assert started.wait(2.0)  # the slow provider's thread did start (and is now blocked)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and [c["name"] for c in registry.snapshot()] != ["fast"]:
        time.sleep(0.02)
    assert [c["name"] for c in registry.snapshot()] == ["fast"]  # fast published despite slow hanging


def test_subscribe_yields_initial_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    registry._poll_provider("p1")
    first = next(registry.subscribe())
    assert first["type"] == "snapshot"
    assert [a["name"] for a in first["agents"]] == ["alpha"]
