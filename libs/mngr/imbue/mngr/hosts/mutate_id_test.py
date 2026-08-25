import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import Field

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.mutate_id import IdMutationError
from imbue.mngr.hosts.mutate_id import mutate_agent_id
from imbue.mngr.hosts.mutate_id import mutate_host_id
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance


def _make_local_host(local_provider: LocalProviderInstance) -> Host:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    return host


class _AgentDataRecordingProvider(LocalProviderInstance):
    """Local provider that records external agent-data store calls."""

    persisted_agent_ids: list[str] = Field(
        default_factory=list, description="Ids of agents persisted to the external store"
    )
    removed_agent_ids: list[str] = Field(
        default_factory=list, description="Ids of agents removed from the external store"
    )

    def persist_agent_data(self, host_id: HostId, agent_data: Mapping[str, object]) -> None:
        self.persisted_agent_ids.append(str(agent_data.get("id")))

    def remove_persisted_agent_data(self, host_id: HostId, agent_id: AgentId) -> None:
        self.removed_agent_ids.append(str(agent_id))


def _write_agent_state(
    host_dir: Path, agent_id: AgentId, name: str, extra: Mapping[str, object] | None = None
) -> Path:
    agent_dir = host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"id": str(agent_id), "name": name, "type": "command", "command": "sleep 68231"}
    if extra:
        data.update(extra)
    (agent_dir / "data.json").write_text(json.dumps(data))
    return agent_dir


@pytest.mark.tmux
def test_mutate_agent_id_renames_the_state_dir_and_rewrites_the_id_field(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    old_id = AgentId.generate()
    new_id = AgentId.generate()
    old_dir = _write_agent_state(local_provider.host_dir, old_id, f"mut-{uuid4().hex}", extra={"future_field": "kept"})
    (old_dir / "events").mkdir()
    (old_dir / "events" / "marker.jsonl").write_text("{}\n")

    mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)

    new_dir = local_provider.host_dir / "agents" / str(new_id)
    assert not old_dir.exists()
    written = json.loads((new_dir / "data.json").read_text())
    assert written["id"] == str(new_id)
    # Every other field -- including ones this mngr version does not know -- survives.
    assert written["future_field"] == "kept"
    assert written["command"] == "sleep 68231"
    # Non-data.json state rides along with the directory rename.
    assert (new_dir / "events" / "marker.jsonl").exists()


def test_mutate_agent_id_converges_after_an_interrupted_rename(local_provider: LocalProviderInstance) -> None:
    host = _make_local_host(local_provider)
    old_id = AgentId.generate()
    new_id = AgentId.generate()
    # Simulate a crash after the mv but before the data.json rewrite: the dir
    # already has the new name but its data.json still carries the old id.
    new_dir = local_provider.host_dir / "agents" / str(new_id)
    new_dir.mkdir(parents=True)
    (new_dir / "data.json").write_text(json.dumps({"id": str(old_id), "name": f"mut-{uuid4().hex}"}))

    mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)

    assert json.loads((new_dir / "data.json").read_text())["id"] == str(new_id)


@pytest.mark.tmux
def test_mutate_agent_id_is_idempotent_once_complete(local_provider: LocalProviderInstance) -> None:
    host = _make_local_host(local_provider)
    old_id = AgentId.generate()
    new_id = AgentId.generate()
    _write_agent_state(local_provider.host_dir, old_id, f"mut-{uuid4().hex}")

    mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)
    mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)

    assert json.loads((local_provider.host_dir / "agents" / str(new_id) / "data.json").read_text())["id"] == str(
        new_id
    )


@pytest.mark.tmux
def test_mutate_agent_id_rekeys_the_externally_persisted_agent_copy(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext
) -> None:
    provider = _AgentDataRecordingProvider(
        name=ProviderInstanceName("local"), host_dir=temp_host_dir, mngr_ctx=temp_mngr_ctx
    )
    host = _make_local_host(provider)
    old_id = AgentId.generate()
    new_id = AgentId.generate()
    _write_agent_state(provider.host_dir, old_id, f"mut-{uuid4().hex}")

    mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)

    assert provider.persisted_agent_ids == [str(new_id)]
    assert provider.removed_agent_ids == [str(old_id)]


def test_mutate_agent_id_refuses_when_the_target_id_is_a_different_agent(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    old_id = AgentId.generate()
    new_id = AgentId.generate()
    _write_agent_state(local_provider.host_dir, old_id, f"mut-{uuid4().hex}")
    _write_agent_state(local_provider.host_dir, new_id, f"mut-{uuid4().hex}")

    with pytest.raises(IdMutationError, match="already exists"):
        mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)


def test_mutate_agent_id_refuses_when_no_source_agent_exists(local_provider: LocalProviderInstance) -> None:
    host = _make_local_host(local_provider)
    with pytest.raises(IdMutationError, match="no such agent"):
        mutate_agent_id(host, AgentId.generate(), AgentId.generate(), host.mngr_ctx.config)


def test_mutate_agent_id_refuses_to_rewrite_an_unrelated_agents_dir(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    old_id = AgentId.generate()
    new_id = AgentId.generate()
    unrelated_id = AgentId.generate()
    new_dir = local_provider.host_dir / "agents" / str(new_id)
    new_dir.mkdir(parents=True)
    (new_dir / "data.json").write_text(json.dumps({"id": str(unrelated_id), "name": f"mut-{uuid4().hex}"}))

    with pytest.raises(IdMutationError, match="belongs to agent"):
        mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)


def test_mutate_agent_id_refuses_a_source_dir_with_a_foreign_id_without_moving_it(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    old_id = AgentId.generate()
    unrelated_id = AgentId.generate()
    # The dir sits at old_id's path but its data.json claims another agent.
    old_dir = _write_agent_state(local_provider.host_dir, old_id, f"mut-{uuid4().hex}")
    (old_dir / "data.json").write_text(json.dumps({"id": str(unrelated_id), "name": f"mut-{uuid4().hex}"}))
    new_id = AgentId.generate()

    with pytest.raises(IdMutationError, match="belongs to agent"):
        mutate_agent_id(host, old_id, new_id, host.mngr_ctx.config)

    # The refusal happened before the rename: the dir stays put, so the
    # mismatch can be inspected and fixed rather than left half-mutated.
    assert old_dir.exists()
    assert not (local_provider.host_dir / "agents" / str(new_id)).exists()


def test_mutate_agent_id_refuses_the_identity_mutation_onto_itself(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    same_id = AgentId.generate()
    with pytest.raises(IdMutationError, match="onto itself"):
        mutate_agent_id(host, same_id, same_id, host.mngr_ctx.config)


@pytest.mark.tmux
def test_mutate_agent_id_refuses_while_the_agent_session_is_running(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    old_id = AgentId.generate()
    agent_name = f"mutid-{uuid4().hex}"
    _write_agent_state(local_provider.host_dir, old_id, agent_name)
    # A local host records no certified tmux prefix, so the guard derives the
    # session name from the calling config's prefix + name rule -- the same
    # rule the real session was created with (the fixture prefix is unique
    # per test, so a hard-coded "mngr-" would never match it).
    session_name = local_provider.mngr_ctx.config.agent_session_name(agent_name)
    subprocess.run(("tmux", "new-session", "-d", "-s", session_name, "sleep", "68232"), check=True)
    try:
        with pytest.raises(IdMutationError, match="running tmux session"):
            mutate_agent_id(host, old_id, AgentId.generate(), host.mngr_ctx.config)
        # The state was left untouched by the refusal.
        assert (local_provider.host_dir / "agents" / str(old_id) / "data.json").exists()
    finally:
        subprocess.run(("tmux", "kill-session", "-t", f"={session_name}"), check=False)


def test_mutate_host_id_restamps_and_preserves_every_other_field(
    local_provider: LocalProviderInstance,
) -> None:
    host = _make_local_host(local_provider)
    old_host_id = HostId.generate()
    new_host_id = HostId.generate()
    data_path = local_provider.host_dir / "data.json"
    data_path.write_text(json.dumps({"host_id": str(old_host_id), "host_name": "old-machine", "future_field": 7}))

    mutate_host_id(host, new_host_id)

    written = json.loads(data_path.read_text())
    assert written["host_id"] == str(new_host_id)
    assert written["host_name"] == "old-machine"
    assert written["future_field"] == 7


def test_mutate_host_id_is_a_no_op_when_already_stamped(local_provider: LocalProviderInstance) -> None:
    host = _make_local_host(local_provider)
    target_id = HostId.generate()
    data_path = local_provider.host_dir / "data.json"
    data_path.write_text(json.dumps({"host_id": str(target_id), "marker": "untouched"}))
    before = data_path.read_text()

    mutate_host_id(host, target_id)

    assert data_path.read_text() == before


def test_mutate_host_id_raises_when_no_data_json_exists(local_provider: LocalProviderInstance) -> None:
    host = _make_local_host(local_provider)
    data_path = local_provider.host_dir / "data.json"
    if data_path.exists():
        data_path.unlink()
    with pytest.raises(IdMutationError, match="no data.json"):
        mutate_host_id(host, HostId.generate())
