"""Unit tests for :mod:`imbue.minds.desktop_client.latchkey_auto_register`."""

import json
import threading
from pathlib import Path

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.testing import leave_permissions_on_this_computer
from imbue.minds.desktop_client.latchkey_auto_register import LatchkeyAutoRegister
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.testing import make_full_fake_latchkey


def _make_discovered(host_id: HostId, agent_id: AgentId) -> DiscoveredAgent:
    """Build a minimal ``DiscoveredAgent`` for resolver fixtures."""
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(f"agent-{str(agent_id)[:8]}"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )


def _push_agents(resolver: MngrCliBackendResolver, *agents: DiscoveredAgent) -> None:
    """Update the resolver with the given agents (and matching id list)."""
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=tuple(a.agent_id for a in agents),
            discovered_agents=tuple(agents),
            ssh_info_by_agent_id={},
        )
    )


def _read_allowed_anyof(plugin_data_dir: Path, host_id: HostId) -> list[dict[str, str]]:
    """Return the ``anyOf`` allow-list for ``host_id`` from its permissions file."""
    config = json.loads(permissions_path_for_host(plugin_data_dir, host_id).read_text())
    return config["schemas"]["minds-api-proxy-per-agent-unauthorized"]["properties"]["path"]["not"]["anyOf"]


def _build_auto_register(
    resolver: MngrCliBackendResolver, latchkey: Latchkey, concurrency_group: ConcurrencyGroup
) -> LatchkeyAutoRegister:
    """An auto-register whose hosts have no machine of their own, for tests about the local edit."""
    return LatchkeyAutoRegister(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=leave_permissions_on_this_computer,
        concurrency_group=concurrency_group,
    )


class _PushRecorder(MutableModel):
    """Records every workspace whose policy the auto-register asked to have pushed, in order."""

    pushed_workspace_agent_ids: list[str] = Field(default_factory=list)
    failing_workspace_agent_ids: frozenset[str] = Field(default_factory=frozenset)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def push(self, workspace_agent_id: str) -> None:
        with self._lock:
            self.pushed_workspace_agent_ids.append(workspace_agent_id)
        if workspace_agent_id in self.failing_workspace_agent_ids:
            raise MachineOperationError(f"machine of {workspace_agent_id} refused the snapshot")

    def pushed_count(self) -> int:
        with self._lock:
            return len(self.pushed_workspace_agent_ids)


def _wait_for_pushes(recorder: _PushRecorder, expected_count: int) -> None:
    assert poll_until(lambda: recorder.pushed_count() >= expected_count, timeout=5.0, poll_interval=0.01), (
        f"expected {expected_count} push(es), saw {recorder.pushed_workspace_agent_ids}"
    )


@pytest.fixture
def resolver() -> MngrCliBackendResolver:
    return MngrCliBackendResolver()


def test_registers_existing_agents_on_start(
    tmp_path: Path, resolver: MngrCliBackendResolver, root_concurrency_group: ConcurrencyGroup
) -> None:
    """``start()`` registers every agent already in the resolver on minds-managed hosts."""
    host_id = HostId.generate()
    seed_agent = AgentId.generate()
    new_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    # Pre-create the host's permissions file with one agent already
    # registered so the host counts as "minds-managed" -- the auto-register
    # callback only touches hosts that already have a permissions file.
    register_agent_for_host(latchkey.plugin_data_dir, host_id, seed_agent)
    _push_agents(resolver, _make_discovered(host_id, new_agent))

    _build_auto_register(resolver, latchkey, root_concurrency_group).start()

    any_of = _read_allowed_anyof(latchkey.plugin_data_dir, host_id)
    registered = {entry["pattern"] for entry in any_of}
    assert any(str(new_agent) in p for p in registered)
    assert any(str(seed_agent) in p for p in registered)


def test_registers_newly_discovered_agents_on_change(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Agents that appear in later discovery ticks get registered without a restart."""
    host_id = HostId.generate()
    seed_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, seed_agent)

    _build_auto_register(resolver, latchkey, root_concurrency_group).start()

    later_agent = AgentId.generate()
    _push_agents(
        resolver,
        _make_discovered(host_id, seed_agent),
        _make_discovered(host_id, later_agent),
    )

    any_of = _read_allowed_anyof(latchkey.plugin_data_dir, host_id)
    registered = {entry["pattern"] for entry in any_of}
    assert any(str(later_agent) in p for p in registered)


def test_does_not_conjure_a_permissions_file_for_an_unmanaged_host(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Hosts that have no existing permissions file do not get one created.

    The file is materialized at host-creation time by
    :func:`finalize_host_permissions`; its absence means the host is not
    minds-managed and we must not conjure one from a discovery event alone.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    _build_auto_register(resolver, latchkey, root_concurrency_group).start()

    assert not permissions_path_for_host(latchkey.plugin_data_dir, host_id).exists()


def test_registers_an_agent_whose_host_file_lands_after_discovery(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A permissions file that appears *after* the agent was discovered still registers it.

    This is the ordering a brand-new workspace actually creates: the agent hits
    the discovery stream before agent creation's ``finalize_host_permissions``
    links the host file into place. Treating that absence as final leaves the
    workspace's own agent out of the host's ``minds-api-proxy`` allowlist for
    the rest of the app's lifetime, so every ``/api/v1/agents/<id>/...`` call
    from inside it is rejected with a 403.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    _build_auto_register(resolver, latchkey, root_concurrency_group).start()
    # Nothing to register against yet -- the deferral must not have written a file.
    assert not permissions_path_for_host(latchkey.plugin_data_dir, host_id).exists()

    # The host file appears, as a regular file -- the shape production lands too:
    # ``link_opaque_permissions_to_host`` promotes the opaque file *to* this path
    # and leaves the symlink on the opaque handle pointing back at it. Either way
    # the retry turns on nothing but ``is_file()`` starting to answer.
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_id)}
    assert any(str(agent_id) in p for p in patterns)


def test_idempotent_across_repeated_discovery_ticks(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Re-firing the same discovery snapshot does not duplicate ``anyOf`` entries.

    ``register_agent_for_host`` is itself idempotent, but this also
    exercises the in-memory dedup set so we know the steady-state
    callback no-ops cleanly.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    other_seed = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, other_seed)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    auto = _build_auto_register(resolver, latchkey, root_concurrency_group)
    auto.start()
    # Fire two more identical discovery ticks.
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    any_of = _read_allowed_anyof(latchkey.plugin_data_dir, host_id)
    patterns = [entry["pattern"] for entry in any_of]
    matches_for_new_agent = [p for p in patterns if str(agent_id) in p]
    assert len(matches_for_new_agent) == 1


def test_handles_multiple_hosts_independently(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Each host's permissions file is updated independently of others."""
    host_a = HostId.generate()
    host_b = HostId.generate()
    agent_a = AgentId.generate()
    agent_b = AgentId.generate()
    seed_a = AgentId.generate()
    seed_b = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_a, seed_a)
    register_agent_for_host(latchkey.plugin_data_dir, host_b, seed_b)
    _push_agents(
        resolver,
        _make_discovered(host_a, agent_a),
        _make_discovered(host_b, agent_b),
    )

    _build_auto_register(resolver, latchkey, root_concurrency_group).start()

    a_patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_a)}
    b_patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_b)}
    assert any(str(agent_a) in p for p in a_patterns)
    assert not any(str(agent_a) in p for p in b_patterns)
    assert any(str(agent_b) in p for p in b_patterns)
    assert not any(str(agent_b) in p for p in a_patterns)


def test_corrupted_permissions_file_logs_but_does_not_retry_forever(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A LatchkeyStoreError is swallowed, and the pair is marked processed.

    The dedup set is updated even on failure so a malformed
    permissions file does not trigger a write attempt on every
    subsequent discovery tick.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    # Bootstrap a valid file then corrupt the anyOf shape so
    # ``register_agent_for_host`` raises ``LatchkeyStoreError``.
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    perms_path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    config = json.loads(perms_path.read_text())
    config["schemas"]["minds-api-proxy-per-agent-unauthorized"]["properties"]["path"]["not"]["anyOf"] = [
        {"pattern": "^/totally/unrecognized$"}
    ]
    perms_path.write_text(json.dumps(config))

    _push_agents(resolver, _make_discovered(host_id, agent_id))

    auto = _build_auto_register(resolver, latchkey, root_concurrency_group)
    # ``start()`` must not raise even though ``register_agent_for_host``
    # raises ``LatchkeyStoreError`` against the corrupted file.
    auto.start()

    # Fire another change: the pair is in the dedup set, so no new
    # write attempt happens. We assert via reading the file -- it
    # should be unchanged from the corrupted state.
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    assert json.loads(perms_path.read_text()) == config


def test_pushes_the_host_policy_to_its_machine_after_a_registration_changes_it(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A registration that changes the host file is carried to the host's machine.

    A remote workspace's gateway enforces its own copy of the policy, so the
    local edit alone would leave that copy behind. The push is addressed by the
    agent just registered, which is how the machine operator finds the
    workspace's machine.
    """
    host_id = HostId.generate()
    new_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _push_agents(resolver, _make_discovered(host_id, new_agent))
    recorder = _PushRecorder()

    LatchkeyAutoRegister(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
        concurrency_group=root_concurrency_group,
    ).start()

    _wait_for_pushes(recorder, 1)
    assert recorder.pushed_workspace_agent_ids == [str(new_agent)]


def test_does_not_push_when_the_registration_changed_nothing(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """An agent already in the allowlist costs no round trip to the machine.

    Every discovered agent is re-registered on app startup; pushing on each of
    those would open every remote workspace's machine for nothing.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, agent_id)
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    recorder = _PushRecorder()

    auto = LatchkeyAutoRegister(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
        concurrency_group=root_concurrency_group,
    )
    auto.start()
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    # The registration is a synchronous no-op, so no push thread was ever
    # started: nothing to wait for, and the recorder must still be empty.
    assert recorder.pushed_workspace_agent_ids == []


def test_a_refused_push_is_logged_and_does_not_stop_later_pushes_for_the_host(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A machine that refuses a snapshot leaves the host eligible for the next push.

    The refusal is not retried here (the next read of the machine brings it up
    to date), but it must release the per-host worker so a later registration
    on the same host is pushed rather than silently dropped.
    """
    host_id = HostId.generate()
    first_agent = AgentId.generate()
    second_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _push_agents(resolver, _make_discovered(host_id, first_agent))
    recorder = _PushRecorder(failing_workspace_agent_ids=frozenset({str(first_agent)}))

    LatchkeyAutoRegister(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
        concurrency_group=root_concurrency_group,
    ).start()
    _wait_for_pushes(recorder, 1)

    _push_agents(resolver, _make_discovered(host_id, first_agent), _make_discovered(host_id, second_agent))

    _wait_for_pushes(recorder, 2)
    assert recorder.pushed_workspace_agent_ids == [str(first_agent), str(second_agent)]
