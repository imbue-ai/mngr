"""Tests for the observe-fed agent registry and its off-thread seed."""

from __future__ import annotations

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


def _fake_agent(agent_id: str, name: str, state: str = "WAITING") -> AgentDetails:
    """Minimal stand-in exposing the fields the registry projections read."""
    return cast(
        AgentDetails,
        SimpleNamespace(
            id=agent_id,
            name=name,
            type="claude",
            state=state,
            host=SimpleNamespace(name="boxa", provider_name="ssh"),
            labels={},
            agent_activity_time=None,
            user_activity_time=None,
            create_time=None,
        ),
    )


def _registry_with_recorded_observe() -> tuple[AgentRegistry, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    ctx = SimpleNamespace(
        concurrency_group=SimpleNamespace(
            run_process_in_background=lambda **kw: calls.append(kw),
        )
    )
    return AgentRegistry(cast(MngrContext, ctx)), calls


def _wait_until(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_seed_runs_off_thread_and_does_not_block_start(monkeypatch: pytest.MonkeyPatch) -> None:
    # The seed must not block start() (hence app.run / the port bind). We make the
    # seed's list_agents hang, then assert start() has already returned with the
    # observe subprocess launched and the map still empty.
    release = threading.Event()
    recorded: dict[str, str] = {}

    def _blocking_list(*_a: object, **_k: object) -> Any:
        recorded["thread"] = threading.current_thread().name
        release.wait(2.0)
        return SimpleNamespace(agents=(_fake_agent("a1", "alpha"),))

    monkeypatch.setattr(ar, "list_agents", _blocking_list)
    registry, calls = _registry_with_recorded_observe()
    registry.start()
    # start() returned while the seed is still blocked -> seed is off the critical path.
    assert registry.snapshot() == []
    assert len(calls) == 1
    assert calls[0]["command"][1:3] == ["observe", "--stream-events"]
    release.set()
    assert _wait_until(lambda: [c["name"] for c in registry.snapshot()] == ["alpha"])
    assert recorded["thread"] != "MainThread"  # ran on the background seed thread


def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ar, "list_agents", lambda *_a, **_k: SimpleNamespace(agents=()))
    registry, calls = _registry_with_recorded_observe()
    registry.start()
    registry.start()
    assert len(calls) == 1  # observe launched exactly once


def test_seed_merges_without_clobbering_observe(monkeypatch: pytest.MonkeyPatch) -> None:
    # If observe already delivered an agent, the seed must not overwrite it (its
    # data is fresher); the seed only fills ids observe has not reported yet.
    registry, _calls = _registry_with_recorded_observe()
    registry._apply_upsert(_fake_agent("a1", "alpha-observe"))
    monkeypatch.setattr(
        ar,
        "list_agents",
        lambda *_a, **_k: SimpleNamespace(agents=(_fake_agent("a1", "alpha-seed"), _fake_agent("a2", "beta"))),
    )
    registry._seed_snapshot()
    assert {c["name"] for c in registry.snapshot()} == {"alpha-observe", "beta"}


def test_seed_notifies_when_it_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    registry, _calls = _registry_with_recorded_observe()
    fired: list[int] = []
    registry.set_on_agents_changed(lambda: fired.append(1))
    monkeypatch.setattr(ar, "list_agents", lambda *_a, **_k: SimpleNamespace(agents=(_fake_agent("a1", "alpha"),)))
    registry._seed_snapshot()
    assert len(fired) == 1  # warm-pool woken so it warms the seeded agent at once


def test_full_state_populates_and_notifies() -> None:
    registry, _calls = _registry_with_recorded_observe()
    fired: list[int] = []
    registry.set_on_agents_changed(lambda: fired.append(1))

    registry._apply_full_state((_fake_agent("a1", "alpha"),))

    assert [c["name"] for c in registry.snapshot()] == ["alpha"]
    assert registry.get_agent("alpha") is not None
    assert len(fired) == 1


def test_upsert_notifies() -> None:
    registry, _calls = _registry_with_recorded_observe()
    fired: list[str] = []
    registry.set_on_agents_changed(lambda: fired.append("x"))

    registry._apply_upsert(_fake_agent("a1", "alpha"))
    registry._apply_upsert(_fake_agent("a2", "beta"))
    assert len(fired) == 2
    assert {c["name"] for c in registry.snapshot()} == {"alpha", "beta"}
