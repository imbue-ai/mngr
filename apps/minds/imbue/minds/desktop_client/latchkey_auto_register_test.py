"""Unit tests for :mod:`imbue.minds.desktop_client.latchkey_auto_register`."""

import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import SYSTEM_SERVICES_AGENT_NAME
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.testing import leave_copies_on_this_computer_as_they_are
from imbue.minds.desktop_client.latchkey.testing import leave_permissions_on_this_computer
from imbue.minds.desktop_client.latchkey_auto_register import LatchkeyAutoRegister
from imbue.minds.desktop_client.latchkey_auto_register import _PendingCarry
from imbue.minds.desktop_client.latchkey_auto_register import _coalesced_carry
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._transfer import adopt_machine_permissions
from imbue.mngr_latchkey.remote.credentials import read_host_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.testing import make_full_fake_latchkey

# A grant another of the user's computers made to a machine, which this computer's copy never held.
_SLACK_RULE_KEY = "slack-api"


def _make_discovered(host_id: HostId, agent_id: AgentId, agent_name: AgentName | None = None) -> DiscoveredAgent:
    """Build a minimal ``DiscoveredAgent`` for resolver fixtures."""
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=agent_name if agent_name is not None else AgentName(f"agent-{str(agent_id)[:8]}"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={},
    )


def _make_services_agent(host_id: HostId, agent_id: AgentId) -> DiscoveredAgent:
    """The system-services agent every workspace runs, whose presence makes a host's listing complete."""
    return _make_discovered(host_id, agent_id, agent_name=AgentName(SYSTEM_SERVICES_AGENT_NAME))


def _push_agents(
    resolver: MngrCliBackendResolver,
    *agents: DiscoveredAgent,
    host_state: HostState | None = None,
    is_replayed_from_before_the_app_started: bool = False,
) -> None:
    """Update the resolver with the agents a discovery snapshot lists, each of their hosts in ``host_state``.

    The snapshot is one this session's discovery took, unless it is said to be replayed.
    """
    host_ids = frozenset(str(a.host_id) for a in agents)
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=tuple(a.agent_id for a in agents),
            discovered_agents=tuple(agents),
            ssh_info_by_agent_id={},
            host_state_by_host_id={} if host_state is None else dict.fromkeys(host_ids, host_state),
            host_ids_reported_this_session=frozenset() if is_replayed_from_before_the_app_started else host_ids,
        )
    )


def _allowed_anyof_of_policy(permissions_json: str) -> list[dict[str, str]]:
    """The ``anyOf`` allow-list of a policy."""
    config = json.loads(permissions_json)
    return config["schemas"]["minds-api-proxy-per-agent-unauthorized"]["properties"]["path"]["not"]["anyOf"]


def _read_allowed_anyof(plugin_data_dir: Path, host_id: HostId) -> list[dict[str, str]]:
    """Return the ``anyOf`` allow-list for ``host_id`` from its permissions file."""
    return _allowed_anyof_of_policy(permissions_path_for_host(plugin_data_dir, host_id).read_text())


def _registered_agent_ids_of_policy(permissions_json: str) -> set[str]:
    """The agent ids a policy registers, read back out of their path patterns."""
    return {
        entry["pattern"].split("/agents/")[1].split("/")[0] for entry in _allowed_anyof_of_policy(permissions_json)
    }


def _registered_agent_ids(plugin_data_dir: Path, host_id: HostId) -> set[str]:
    """The agent ids ``host_id``'s permissions file registers."""
    return _registered_agent_ids_of_policy(permissions_path_for_host(plugin_data_dir, host_id).read_text())


def _rule_keys_of_policy(permissions_json: str) -> list[str]:
    return [next(iter(rule)) for rule in json.loads(permissions_json)["rules"]]


def _policy_with_slack_grant(permissions_json: str) -> str:
    """``permissions_json`` with a Slack grant another computer made, which this computer's copy never held."""
    config = json.loads(permissions_json)
    config["rules"].append({_SLACK_RULE_KEY: ["slack-read-all"]})
    return json.dumps(config)


def _register_all(latchkey: Latchkey, host_id: HostId, *agent_ids: AgentId) -> None:
    for agent_id in agent_ids:
        register_agent_for_host(latchkey.plugin_data_dir, host_id, agent_id)


def _provision_machine(latchkey: Latchkey, host_id: HostId) -> None:
    """Record a machine key for ``host_id``, as the provisioning pass does once the host's gateway is wired."""
    store_machine_encryption_key(latchkey.plugin_data_dir, host_id, SecretStr("machine-key-7c1d"))


def _build_local_auto_register(
    build: Callable[..., LatchkeyAutoRegister], resolver: MngrCliBackendResolver, latchkey: Latchkey
) -> LatchkeyAutoRegister:
    """An auto-register whose hosts have no machine of their own, for tests about the local edit."""
    return build(
        backend_resolver=resolver,
        latchkey=latchkey,
        refresh_permissions_from_machine=leave_copies_on_this_computer_as_they_are,
        push_permissions_to_machine=leave_permissions_on_this_computer,
    )


class _FakeMachine(MutableModel):
    """Stands in for one host's machine: holds the policy last handed to it, and hands it back on a read.

    A read adopts the machine's policy over this computer's copy, as the real
    machine operator does, so a test sees exactly what a push would carry
    after the read; a machine holding nothing yet is seeded from the copy here
    on its first read, as the real read seeds an unprovisioned one. Every read
    and push is recorded by the workspace agent it was addressed by, in order.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    latchkey: Latchkey = Field(frozen=True)
    host_id: HostId = Field(frozen=True)
    policy_json: str | None = Field(default=None, description="The policy the machine holds; None before any push.")
    pushed_workspace_agent_ids: list[str] = Field(default_factory=list)
    refreshed_workspace_agent_ids: list[str] = Field(default_factory=list)
    failing_workspace_agent_ids: frozenset[str] = Field(
        default_factory=frozenset, description="The addresses whose pushes the machine refuses."
    )
    unreadable_workspace_agent_ids: frozenset[str] = Field(
        default_factory=frozenset, description="The addresses whose reads the machine fails."
    )
    during_read: Callable[[], None] | None = Field(
        default=None,
        description="Run while a read is in flight, before its adopt lands: what another thread does meanwhile.",
    )
    read_counts_at_push: list[int] = Field(
        default_factory=list, description="How many reads the machine had taken when each push arrived."
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def refresh(self, workspace_agent_id: str) -> None:
        with self._lock:
            self.refreshed_workspace_agent_ids.append(workspace_agent_id)
            if workspace_agent_id in self.unreadable_workspace_agent_ids:
                raise MachineOperationError(f"machine of {workspace_agent_id} could not be read", None)
            if self.during_read is not None:
                self.during_read()
            if self.policy_json is None:
                self.policy_json = read_host_permissions(self.latchkey.plugin_data_dir, self.host_id)
            else:
                adopt_machine_permissions(self.latchkey.latchkey_directory, self.host_id, self.policy_json)

    def push(self, workspace_agent_id: str) -> None:
        with self._lock:
            self.pushed_workspace_agent_ids.append(workspace_agent_id)
            self.read_counts_at_push.append(len(self.refreshed_workspace_agent_ids))
            if workspace_agent_id in self.failing_workspace_agent_ids:
                raise MachineOperationError(f"machine of {workspace_agent_id} refused the snapshot", None)
            self.policy_json = read_host_permissions(self.latchkey.plugin_data_dir, self.host_id)

    def pushed_count(self) -> int:
        with self._lock:
            return len(self.pushed_workspace_agent_ids)

    def refreshed_count(self) -> int:
        with self._lock:
            return len(self.refreshed_workspace_agent_ids)

    def registered_agent_ids(self) -> set[str]:
        with self._lock:
            assert self.policy_json is not None, "the machine holds no policy"
            return _registered_agent_ids_of_policy(self.policy_json)


def _machine_seeded_from_this_computer(latchkey: Latchkey, host_id: HostId, **fields: frozenset[str]) -> _FakeMachine:
    """A machine holding what this computer's copy of ``host_id``'s policy says now, as a provisioning pass seeds it."""
    return _FakeMachine(
        latchkey=latchkey,
        host_id=host_id,
        policy_json=read_host_permissions(latchkey.plugin_data_dir, host_id),
        **fields,
    )


def _machine_registering_an_agent_this_computer_has_not_seen(
    latchkey: Latchkey, host_id: HostId, other_computers_agent: AgentId
) -> _FakeMachine:
    """A machine registering ``other_computers_agent``, carried to it by another computer, which this computer's
    copy of ``host_id``'s policy does not register."""
    policy_before_the_other_computer_registered = read_host_permissions(latchkey.plugin_data_dir, host_id)
    assert policy_before_the_other_computer_registered is not None
    register_agent_for_host(latchkey.plugin_data_dir, host_id, other_computers_agent)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    permissions_path_for_host(latchkey.plugin_data_dir, host_id).write_text(
        policy_before_the_other_computer_registered
    )
    return machine


def _build_auto_register_carrying_to(
    build: Callable[..., LatchkeyAutoRegister],
    resolver: MngrCliBackendResolver,
    latchkey: Latchkey,
    machine: _FakeMachine,
    **fields: float,
) -> LatchkeyAutoRegister:
    """An auto-register that reads from and pushes to ``machine``, for tests about the carry."""
    return build(
        backend_resolver=resolver,
        latchkey=latchkey,
        refresh_permissions_from_machine=machine.refresh,
        push_permissions_to_machine=machine.push,
        **fields,
    )


def _wait_for_pushes(machine: _FakeMachine, expected_count: int) -> None:
    assert poll_until(lambda: machine.pushed_count() >= expected_count, timeout=5.0, poll_interval=0.01), (
        f"expected {expected_count} push(es), saw {machine.pushed_workspace_agent_ids}"
    )


def _wait_for_reads(machine: _FakeMachine, expected_count: int) -> None:
    assert poll_until(lambda: machine.refreshed_count() >= expected_count, timeout=5.0, poll_interval=0.01), (
        f"expected {expected_count} read(s), saw {machine.refreshed_workspace_agent_ids}"
    )


def _wait_until_push_is_held(auto_register: LatchkeyAutoRegister, host_id: HostId) -> None:
    """Wait for the push worker to find ``host_id`` without a machine and hold its push."""
    assert poll_until(lambda: host_id in auto_register._hosts_awaiting_machine, timeout=5.0, poll_interval=0.01), (
        f"expected the push for host {host_id} to be held for its machine"
    )


def _wait_until_machine_watch_has_ended(auto_register: LatchkeyAutoRegister, host_id: HostId) -> None:
    """Wait for the bounded watch that polls for ``host_id``'s machine key to give up."""
    assert poll_until(
        lambda: host_id not in auto_register._hosts_watched_for_machine, timeout=5.0, poll_interval=0.01
    ), f"expected the machine watch for host {host_id} to have ended"


def _wait_until_pushes_settle(auto_register: LatchkeyAutoRegister) -> None:
    """Wait for every push worker to finish; a change schedules its worker synchronously, so none is outstanding after."""
    assert poll_until(lambda: not auto_register._hosts_being_pushed, timeout=5.0, poll_interval=0.01), (
        f"expected no push to be running, but hosts {auto_register._hosts_being_pushed} are still being pushed"
    )


@pytest.fixture
def resolver() -> MngrCliBackendResolver:
    return MngrCliBackendResolver()


def test_registers_existing_agents_on_start(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """``start()`` registers every agent already in the resolver on hosts Imbue Studio manages."""
    host_id = HostId.generate()
    seed_agent = AgentId.generate()
    new_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    # Pre-create the host's permissions file with one agent already
    # registered so the host counts as managed by Imbue Studio -- the auto-register
    # callback only touches hosts that already have a permissions file.
    register_agent_for_host(latchkey.plugin_data_dir, host_id, seed_agent)
    _push_agents(resolver, _make_discovered(host_id, new_agent))

    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    any_of = _read_allowed_anyof(latchkey.plugin_data_dir, host_id)
    registered = {entry["pattern"] for entry in any_of}
    assert any(str(new_agent) in p for p in registered)
    assert any(str(seed_agent) in p for p in registered)


def test_registers_newly_discovered_agents_on_change(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """Agents that appear in later discovery ticks get registered without a restart."""
    host_id = HostId.generate()
    seed_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, seed_agent)

    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

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
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """Hosts that have no existing permissions file do not get one created.

    The file is materialized at host-creation time by
    :func:`finalize_host_permissions`; its absence means the host is not
    managed by Imbue Studio and we must not conjure one from a discovery event alone.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    assert not permissions_path_for_host(latchkey.plugin_data_dir, host_id).exists()


def test_registers_an_agent_whose_host_file_lands_after_discovery(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
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

    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()
    # Nothing to register against yet -- the deferral must not have written a file.
    assert not permissions_path_for_host(latchkey.plugin_data_dir, host_id).exists()

    # The host file appears, as a regular file -- the shape production lands too:
    # ``link_opaque_permissions_to_host`` promotes the opaque file *to* this path
    # and leaves the symlink on the opaque handle pointing back at it. Either way
    # the retry turns on nothing but ``is_file()`` starting to answer. Agent
    # creation fires a resolver change right after linking it; an unchanged
    # discovery push would not.
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    resolver.notify_change()

    patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_id)}
    assert any(str(agent_id) in p for p in patterns)


def test_idempotent_across_repeated_resolver_changes(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """Resolver changes that leave the discovered agents alone do not duplicate ``anyOf`` entries.

    Pinning is idempotent, and a host whose listing has not moved is not
    even read again, so the steady-state callback no-ops cleanly.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    other_seed = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, other_seed)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    auto = _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey)
    auto.start()
    # Two more resolver changes that leave the discovered agents as they are.
    resolver.notify_change()
    resolver.notify_change()

    any_of = _read_allowed_anyof(latchkey.plugin_data_dir, host_id)
    patterns = [entry["pattern"] for entry in any_of]
    matches_for_new_agent = [p for p in patterns if str(agent_id) in p]
    assert len(matches_for_new_agent) == 1


def test_handles_multiple_hosts_independently(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
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

    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    a_patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_a)}
    b_patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_b)}
    assert any(str(agent_a) in p for p in a_patterns)
    assert not any(str(agent_a) in p for p in b_patterns)
    assert any(str(agent_b) in p for p in b_patterns)
    assert not any(str(agent_b) in p for p in a_patterns)


def test_corrupted_permissions_file_logs_but_does_not_retry_forever(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A LatchkeyStoreError is swallowed, and the host counts as reconciled against that listing.

    The listing is recorded even on failure so a malformed permissions file
    does not trigger a write attempt on every subsequent resolver change,
    only when discovery next says something new about the host.
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

    auto = _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey)
    # ``start()`` must not raise even though ``register_agent_for_host``
    # raises ``LatchkeyStoreError`` against the corrupted file.
    auto.start()

    # Fire another change: the pair is in the dedup set, so no new
    # write attempt happens. We assert via reading the file -- it
    # should be unchanged from the corrupted state.
    resolver.notify_change()
    assert json.loads(perms_path.read_text()) == config


def test_pushes_the_host_policy_to_its_machine_after_a_registration_changes_it(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
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
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    _push_agents(resolver, _make_discovered(host_id, new_agent))

    _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine).start()

    _wait_for_pushes(machine, 1)
    assert machine.pushed_workspace_agent_ids == [str(new_agent)]
    assert str(new_agent) in machine.registered_agent_ids()


def test_a_grant_only_the_machine_holds_survives_the_push_of_a_registration(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A registration is carried as a read-modify-write, so a grant another computer made to the machine stays.

    The machine owns the policy, and this computer's copy can lag it by
    whatever the user's other computers granted since this one last read it.
    Pushing that copy as it stood after the registration replaced the
    machine's grants with it; reading first, pinning the agents again on what
    was read, and pushing that keeps the grant and registers the agent.
    """
    host_id = HostId.generate()
    new_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    assert machine.policy_json is not None
    machine.policy_json = _policy_with_slack_grant(machine.policy_json)
    _push_agents(resolver, _make_discovered(host_id, new_agent))

    _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine).start()

    _wait_for_pushes(machine, 1)
    assert machine.refreshed_workspace_agent_ids == [str(new_agent)]
    assert machine.policy_json is not None
    assert _SLACK_RULE_KEY in _rule_keys_of_policy(machine.policy_json)
    assert str(new_agent) in machine.registered_agent_ids()
    local_policy = read_host_permissions(latchkey.plugin_data_dir, host_id)
    assert local_policy is not None
    assert _SLACK_RULE_KEY in _rule_keys_of_policy(local_policy)


def test_a_read_a_reconcile_wrote_during_is_repeated_so_the_pin_is_on_the_machines_policy(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A reconcile that rewrites the file while the machine is being read cannot put the stale copy back.

    The read runs outside the reconcile lock, so a reconcile can read the
    file before the adopt lands and write after it, leaving this computer's
    stale copy over the machine's policy; a carry that pinned on that would
    push the stale copy and lose the machine's grant. The carry notices the
    reconcile's write and reads the machine again before pinning.
    """
    host_id = HostId.generate()
    first_agent, second_agent = AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    assert machine.policy_json is not None
    machine.policy_json = _policy_with_slack_grant(machine.policy_json)
    # Discovery lists a second agent while the first carry's read is in flight, so
    # a reconcile rewrites the file between that read's start and its adopt.
    machine.during_read = lambda: _push_agents(
        resolver, _make_discovered(host_id, first_agent), _make_discovered(host_id, second_agent)
    )
    _push_agents(resolver, _make_discovered(host_id, first_agent))

    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()

    _wait_for_pushes(machine, 2)
    _wait_until_pushes_settle(auto_register)
    # The first carry read twice before its push (the reconcile wrote during the first read); the second carry,
    # scheduled by that reconcile, read once more and carried the second agent.
    assert machine.read_counts_at_push == [2, 3]
    assert machine.policy_json is not None
    assert _SLACK_RULE_KEY in _rule_keys_of_policy(machine.policy_json)
    assert {str(first_agent), str(second_agent)} <= machine.registered_agent_ids()
    local_policy = read_host_permissions(latchkey.plugin_data_dir, host_id)
    assert local_policy is not None
    assert _SLACK_RULE_KEY in _rule_keys_of_policy(local_policy)


def test_an_agent_only_the_machine_registers_survives_the_push_of_a_registration(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """The pin is applied again on what the machine holds, so a registration another computer carried to it stays.

    That agent is not in this computer's listing yet, and a registered agent
    is dropped only once two complete listings lack it; a pin of the list this
    computer's copy produced would have taken it out of the machine's policy.
    """
    host_id = HostId.generate()
    new_agent = AgentId.generate()
    other_computers_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _provision_machine(latchkey, host_id)
    machine = _machine_registering_an_agent_this_computer_has_not_seen(latchkey, host_id, other_computers_agent)
    _push_agents(resolver, _make_discovered(host_id, new_agent))

    _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine).start()

    _wait_for_pushes(machine, 1)
    assert {str(new_agent), str(other_computers_agent)} <= machine.registered_agent_ids()
    assert {str(new_agent), str(other_computers_agent)} <= _registered_agent_ids(latchkey.plugin_data_dir, host_id)


def test_a_machine_that_cannot_be_read_is_not_pushed(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A push without a read first would replace whatever the machine holds, so a failed read pushes nothing.

    The registration still stands in this computer's copy, which the desktop
    gateway's own per-agent gate reads.
    """
    host_id = HostId.generate()
    new_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(
        latchkey, host_id, unreadable_workspace_agent_ids=frozenset({str(new_agent)})
    )
    _push_agents(resolver, _make_discovered(host_id, new_agent))

    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()

    _wait_for_reads(machine, 1)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == []
    assert str(new_agent) not in machine.registered_agent_ids()
    assert str(new_agent) in _registered_agent_ids(latchkey.plugin_data_dir, host_id)


def test_a_machine_that_already_registers_the_pinned_agents_is_not_pushed(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A registration another computer already carried to the machine costs one read and no push."""
    host_id = HostId.generate()
    new_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _provision_machine(latchkey, host_id)
    machine = _machine_registering_an_agent_this_computer_has_not_seen(latchkey, host_id, new_agent)
    machine_policy_before = machine.policy_json
    _push_agents(resolver, _make_discovered(host_id, new_agent))

    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()

    _wait_for_reads(machine, 1)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == []
    assert machine.policy_json == machine_policy_before


def test_does_not_push_when_the_registration_changed_nothing(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """An agent already in the allowlist costs no round trip to the machine.

    Every discovered agent is re-registered on app startup; pushing on each of
    those would open every remote workspace's machine for nothing.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, agent_id)
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    auto = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto.start()
    resolver.notify_change()

    # The registration is a synchronous no-op, so no push thread was ever
    # started: nothing to wait for, and the machine must not have been touched.
    assert machine.refreshed_workspace_agent_ids == []
    assert machine.pushed_workspace_agent_ids == []


def test_a_refused_push_is_logged_and_does_not_stop_later_pushes_for_the_host(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
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
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(
        latchkey, host_id, failing_workspace_agent_ids=frozenset({str(first_agent)})
    )
    _push_agents(resolver, _make_discovered(host_id, first_agent))

    _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine).start()
    _wait_for_pushes(machine, 1)

    _push_agents(resolver, _make_discovered(host_id, first_agent), _make_discovered(host_id, second_agent))

    _wait_for_pushes(machine, 2)
    assert len(machine.pushed_workspace_agent_ids) == 2
    assert machine.pushed_workspace_agent_ids[0] == str(first_agent)
    assert machine.pushed_workspace_agent_ids[1] in {str(first_agent), str(second_agent)}


def test_a_registration_made_before_the_machine_is_provisioned_is_pushed_once_it_is(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A push that finds no machine yet is held, and its watch carries it once the machine's key is recorded.

    The order a new remote workspace actually creates: the host file (and the
    registration into it) lands when ``mngr create`` returns, and the
    provisioning pass records the machine's key seconds later, from a process
    that tells the resolver nothing. A push dropped in between would leave the
    gateway enforcing the deny-all default it was seeded with, refusing the
    workspace's own agent.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    auto_register = _build_auto_register_carrying_to(
        build_latchkey_auto_register, resolver, latchkey, machine, machine_poll_interval_seconds=0.05
    )
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    # A resolver change before the machine exists carries nothing, and does not drop the push either.
    resolver.notify_change()
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == []
    assert host_id in auto_register._hosts_awaiting_machine

    # No resolver change follows the key: the watch alone carries the push.
    _provision_machine(latchkey, host_id)

    _wait_for_pushes(machine, 1)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == [str(agent_id)]
    assert str(agent_id) in machine.registered_agent_ids()
    # The held push is consumed: later changes do not push it again.
    resolver.notify_change()
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == [str(agent_id)]
    assert host_id not in auto_register._hosts_awaiting_machine


def test_a_held_push_is_carried_by_a_resolver_change_after_its_watch_has_ended(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """Once the bounded watch is over, the next resolver change that finds the machine's key carries the push."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    auto_register = _build_auto_register_carrying_to(
        build_latchkey_auto_register,
        resolver,
        latchkey,
        machine,
        machine_poll_interval_seconds=0.01,
        machine_watch_seconds=0.0,
    )
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    _wait_until_machine_watch_has_ended(auto_register, host_id)

    _provision_machine(latchkey, host_id)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == []

    resolver.notify_change()
    _wait_for_pushes(machine, 1)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == [str(agent_id)]
    assert host_id not in auto_register._hosts_awaiting_machine


def test_a_registration_on_a_host_with_no_machine_is_never_pushed(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A local host's gateway reads the file here, so the held push for it is never carried anywhere, and its
    watch ends rather than polling for the app's lifetime."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    machine = _FakeMachine(latchkey=latchkey, host_id=host_id)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    auto_register = _build_auto_register_carrying_to(
        build_latchkey_auto_register,
        resolver,
        latchkey,
        machine,
        machine_poll_interval_seconds=0.01,
        machine_watch_seconds=0.1,
    )
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    _wait_until_machine_watch_has_ended(auto_register, host_id)
    resolver.notify_change()
    resolver.notify_change()
    _wait_until_pushes_settle(auto_register)

    assert machine.refreshed_workspace_agent_ids == []
    assert machine.pushed_workspace_agent_ids == []
    assert host_id in auto_register._hosts_awaiting_machine


def test_a_registration_on_a_stopped_host_is_pushed_once_the_host_runs_again(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A stopped workspace is still listed, but its provider has no machine to open for it, so its push is held
    without touching the machine and carried by the resolver change that reports the host running.

    No watch polls for it meanwhile: the host's state reaches the resolver, unlike a machine key.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    agent = _make_discovered(host_id, agent_id)
    _push_agents(resolver, agent, host_state=HostState.STOPPED)

    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    _wait_until_pushes_settle(auto_register)
    assert host_id not in auto_register._hosts_watched_for_machine
    assert str(agent_id) in _registered_agent_ids(latchkey.plugin_data_dir, host_id)

    # A host on its way up has no machine to open yet either.
    _push_agents(resolver, agent, host_state=HostState.STARTING)
    _wait_until_pushes_settle(auto_register)
    assert machine.refreshed_workspace_agent_ids == []
    assert machine.pushed_workspace_agent_ids == []
    assert host_id in auto_register._hosts_awaiting_machine

    _push_agents(resolver, agent, host_state=HostState.RUNNING)

    _wait_for_pushes(machine, 1)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == [str(agent_id)]
    assert str(agent_id) in machine.registered_agent_ids()
    assert host_id not in auto_register._hosts_awaiting_machine


def test_a_registration_on_a_host_only_a_replayed_snapshot_reports_waits_for_this_sessions_discovery(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """At startup the resolver holds what the events file kept from before the app started, in which a workspace
    that has stopped since still reads as running. Its push is held until this session's discovery has reported
    the host's provider, and then goes by what that says."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    agent = _make_discovered(host_id, agent_id)
    _push_agents(resolver, agent, host_state=HostState.RUNNING, is_replayed_from_before_the_app_started=True)

    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    _wait_until_pushes_settle(auto_register)
    assert machine.refreshed_workspace_agent_ids == []
    assert host_id not in auto_register._hosts_watched_for_machine

    # This session's first look finds the workspace stopped: there is still no machine to open.
    _push_agents(resolver, agent, host_state=HostState.STOPPED)
    _wait_until_pushes_settle(auto_register)
    assert machine.refreshed_workspace_agent_ids == []
    assert machine.pushed_workspace_agent_ids == []
    assert host_id in auto_register._hosts_awaiting_machine

    _push_agents(resolver, agent, host_state=HostState.RUNNING)

    _wait_for_pushes(machine, 1)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == [str(agent_id)]
    assert host_id not in auto_register._hosts_awaiting_machine


def test_a_held_push_whose_machine_key_lands_while_the_host_is_down_waits_for_the_host(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A machine key alone does not release a held push: the watch that finds it leaves the push held while the
    host is down, and the resolver change that reports the host running carries it."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    agent = _make_discovered(host_id, agent_id)
    _push_agents(resolver, agent, host_state=HostState.STOPPED)

    auto_register = _build_auto_register_carrying_to(
        build_latchkey_auto_register,
        resolver,
        latchkey,
        machine,
        machine_poll_interval_seconds=0.01,
        machine_watch_seconds=0.2,
    )
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    _provision_machine(latchkey, host_id)
    _wait_until_machine_watch_has_ended(auto_register, host_id)
    _wait_until_pushes_settle(auto_register)
    assert machine.refreshed_workspace_agent_ids == []
    assert machine.pushed_workspace_agent_ids == []
    assert host_id in auto_register._hosts_awaiting_machine

    _push_agents(resolver, agent, host_state=HostState.RUNNING)

    _wait_for_pushes(machine, 1)
    _wait_until_pushes_settle(auto_register)
    assert machine.pushed_workspace_agent_ids == [str(agent_id)]
    assert host_id not in auto_register._hosts_awaiting_machine


def test_a_push_scheduled_while_the_app_shuts_down_is_kept_without_a_worker(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A group that is shutting down refuses the push worker, and the refusal stays inside the auto-register.

    The schedule runs on the resolver's callback thread, which lets anything
    but ``OSError`` and ``RuntimeError`` through, so the group's refusal must
    not escape the change that registered the agent.
    """
    host_id = HostId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, AgentId.generate())
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()
    root_concurrency_group.shutdown()

    agent_id = AgentId.generate()
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    assert machine.pushed_workspace_agent_ids == []
    assert set(auto_register._hosts_awaiting_push) == {host_id}
    pending = auto_register._hosts_awaiting_push[host_id]
    assert pending.address_agent_id == agent_id
    assert pending.listed_agent_ids == {agent_id}
    assert pending.dropped_agent_ids == frozenset()
    assert host_id not in auto_register._hosts_being_pushed


def _report_listing(resolver: MngrCliBackendResolver, *agents: DiscoveredAgent) -> None:
    """Apply a discovery snapshot and wake the listeners even when it reads like the last one.

    An unchanged agents snapshot fires no change callback by itself; in
    production the provider snapshot applied beside it does, which is how a
    second look at the same listing reaches the auto-register.
    """
    _push_agents(resolver, *agents)
    resolver.notify_change()


def test_a_single_complete_listing_lacking_an_agent_keeps_it_registered(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """One complete listing can be wrong about one agent (a record that could not be read), so it drops nothing yet."""
    host_id = HostId.generate()
    services, live, gone = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))

    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live), str(gone)}


def test_an_agent_two_consecutive_complete_listings_lack_is_unregistered(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """The second complete listing from a newer snapshot confirms the first: the agent is gone."""
    host_id = HostId.generate()
    services, live, gone = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    _report_listing(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))

    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live)}


def test_an_agent_a_later_listing_drops_is_unregistered_once_the_listing_after_confirms_it(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    host_id = HostId.generate()
    services, live, doomed = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services)
    _push_agents(
        resolver,
        _make_services_agent(host_id, services),
        _make_discovered(host_id, live),
        _make_discovered(host_id, doomed),
    )
    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()
    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live), str(doomed)}

    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live), str(doomed)}
    _report_listing(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))

    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live)}


def test_an_agent_listed_again_before_the_second_look_stays_registered_and_starts_over(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A listing that lacks an agent once and lists it again was the wrong one; the count of looks restarts."""
    host_id = HostId.generate()
    services, live, flickering = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, flickering)
    complete_without = (_make_services_agent(host_id, services), _make_discovered(host_id, live))
    complete_with = (*complete_without, _make_discovered(host_id, flickering))
    _push_agents(resolver, *complete_without)
    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    _report_listing(resolver, *complete_with)
    assert str(flickering) in _registered_agent_ids(latchkey.plugin_data_dir, host_id)
    _report_listing(resolver, *complete_without)
    assert str(flickering) in _registered_agent_ids(latchkey.plugin_data_dir, host_id)
    _report_listing(resolver, *complete_without)

    assert str(flickering) not in _registered_agent_ids(latchkey.plugin_data_dir, host_id)


def test_a_listing_without_the_services_agent_drops_nothing_and_carries_the_pending_drop(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A partial listing (or a stopped host) proves nothing gone, but does not make the next complete one start over."""
    host_id = HostId.generate()
    services, live, gone = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    _report_listing(resolver, _make_discovered(host_id, live))
    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live), str(gone)}
    _report_listing(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))

    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live)}


def test_a_resolver_change_without_a_new_snapshot_is_not_a_second_look(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """Resolver changes fire on far more than discovery; only a newer snapshot confirms an absence.

    This is also what protects an agent registered outside discovery (a file
    repair, the CLI), which no listing has caught up with yet.
    """
    host_id = HostId.generate()
    services, live, gone, registered_by_hand = (AgentId.generate() for _ in range(4))
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()

    register_agent_for_host(latchkey.plugin_data_dir, host_id, registered_by_hand)
    resolver.notify_change()
    resolver.notify_change()

    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {
        str(services),
        str(live),
        str(gone),
        str(registered_by_hand),
    }


def test_pushes_a_pinned_policy_to_the_hosts_machine_addressed_by_its_services_agent(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """The machine enforces its own copy, so a dead id stays allowed there until the pinned snapshot reaches it."""
    host_id = HostId.generate()
    services, live, gone = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine).start()
    assert machine.pushed_workspace_agent_ids == []

    _report_listing(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))

    _wait_for_pushes(machine, 1)
    assert machine.pushed_workspace_agent_ids == [str(services)]
    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live)}
    assert machine.registered_agent_ids() == {str(services), str(live)}


def test_a_carry_scheduled_over_one_still_waiting_keeps_the_drop_only_the_earlier_one_knew(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """Two reconciles that schedule while one carry runs fold into one carry that drops what either one dropped.

    The machine's policy is read back before the pin and still registers the
    dropped agent; only the reconcile that dropped it knew to take it out, so
    a carry that replaced that one would have left the agent registered there.
    The group is shut down so no worker claims the first carry before the
    second is scheduled.
    """
    host_id = HostId.generate()
    services, live, gone, new = AgentId.generate(), AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    _provision_machine(latchkey, host_id)
    machine = _machine_seeded_from_this_computer(latchkey, host_id)
    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()
    root_concurrency_group.shutdown()

    _report_listing(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    assert auto_register._hosts_awaiting_push[host_id].dropped_agent_ids == {gone}
    _push_agents(
        resolver,
        _make_services_agent(host_id, services),
        _make_discovered(host_id, live),
        _make_discovered(host_id, new),
    )

    pending = auto_register._hosts_awaiting_push[host_id]
    assert pending.address_agent_id == services
    assert pending.listed_agent_ids == {services, live, new}
    assert pending.dropped_agent_ids == {gone}
    assert machine.pushed_workspace_agent_ids == []


def test_a_carry_folded_into_a_later_one_keeps_the_agents_only_it_listed_unless_the_later_one_dropped_them() -> None:
    """The earlier carry's listing survives the fold, minus what the later carry dropped.

    An agent only the earlier listing had, which the later complete listing
    lacked for the first time, is still registered in the file here (a drop
    takes two complete listings), and the machine never received the earlier
    carry; a pin of the later listing alone would unregister it on the machine
    and, through the adopt, here, after one absence. An agent the later carry
    dropped is taken out all the same, listed earlier or not.
    """
    services, absent_once, dropped_later, new = (
        AgentId.generate(),
        AgentId.generate(),
        AgentId.generate(),
        AgentId.generate(),
    )
    earlier = _PendingCarry(
        address_agent_id=services,
        listed_agent_ids=frozenset({services, absent_once, dropped_later}),
        dropped_agent_ids=frozenset(),
    )
    later = _PendingCarry(
        address_agent_id=services,
        listed_agent_ids=frozenset({services, new}),
        dropped_agent_ids=frozenset({dropped_later}),
    )

    folded = _coalesced_carry(earlier, later)

    assert folded.address_agent_id == services
    assert folded.listed_agent_ids == {services, absent_once, new}
    assert folded.dropped_agent_ids == {dropped_later}


def test_a_carry_held_over_one_already_held_for_a_machine_keeps_the_drop_only_the_earlier_one_knew(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    root_concurrency_group: ConcurrencyGroup,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A worker holding a carry for a host with no machine yet folds it into the carry already held there.

    A reconcile that schedules between the worker's claim of a carry and its
    hold finds nothing to fold into, so it leaves its carry waiting beside the
    held one; the worker holds that one next, and replacing the earlier carry
    would lose the drop only it knew of. The two carries are placed where that
    race leaves them, and the group is shut down so the worker's body runs
    here, with no thread of its own and no watch started.
    """
    host_id = HostId.generate()
    services, live, gone, new = AgentId.generate(), AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    machine = _FakeMachine(latchkey=latchkey, host_id=host_id)
    auto_register = _build_auto_register_carrying_to(build_latchkey_auto_register, resolver, latchkey, machine)
    auto_register.start()
    root_concurrency_group.shutdown()
    earlier = _PendingCarry(
        address_agent_id=services, listed_agent_ids=frozenset({services, live}), dropped_agent_ids=frozenset({gone})
    )
    later = _PendingCarry(
        address_agent_id=services, listed_agent_ids=frozenset({services, live, new}), dropped_agent_ids=frozenset()
    )
    with auto_register._push_lock:
        auto_register._hosts_awaiting_machine[host_id] = earlier
        auto_register._hosts_awaiting_push[host_id] = later
        auto_register._hosts_being_pushed.add(host_id)

    auto_register._push_until_settled(host_id)

    held = auto_register._hosts_awaiting_machine[host_id]
    assert held.listed_agent_ids == {services, live, new}
    assert held.dropped_agent_ids == {gone}
    assert host_id not in auto_register._hosts_being_pushed
    assert machine.refreshed_workspace_agent_ids == []
    assert machine.pushed_workspace_agent_ids == []


def test_a_carry_scheduled_over_a_held_and_a_waiting_one_folds_them_oldest_first(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """The held carry was claimed before the waiting one was scheduled, and both before the new one.

    An agent the held carry listed, the waiting carry lacked for the first
    time, and the new carry then dropped must stay out of the listing, as it
    would after the drop; folding the new carry in ahead of the held one would
    put it back and the pin would register it again. The worker slot is marked
    taken so the scheduling starts no worker, leaving the folded carry where
    the test can read it.
    """
    host_id = HostId.generate()
    services, dropped_later, new = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services)
    auto_register = _build_auto_register_carrying_to(
        build_latchkey_auto_register, resolver, latchkey, _FakeMachine(latchkey=latchkey, host_id=host_id)
    )
    held = _PendingCarry(
        address_agent_id=services, listed_agent_ids=frozenset({services, dropped_later}), dropped_agent_ids=frozenset()
    )
    waiting = _PendingCarry(
        address_agent_id=services, listed_agent_ids=frozenset({services}), dropped_agent_ids=frozenset()
    )
    newest = _PendingCarry(
        address_agent_id=services,
        listed_agent_ids=frozenset({services, new}),
        dropped_agent_ids=frozenset({dropped_later}),
    )
    with auto_register._push_lock:
        auto_register._hosts_awaiting_machine[host_id] = held
        auto_register._hosts_awaiting_push[host_id] = waiting
        auto_register._hosts_being_pushed.add(host_id)

    auto_register._schedule_push(host_id, newest)

    folded = auto_register._hosts_awaiting_push[host_id]
    assert folded.listed_agent_ids == {services, new}
    assert folded.dropped_agent_ids == {dropped_later}
    assert host_id not in auto_register._hosts_awaiting_machine


def test_a_held_carry_carried_over_a_waiting_one_is_folded_in_as_the_older_of_the_two(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A held carry its machine's appearance brings back is older than a carry scheduled while it was held.

    An agent the held carry dropped and the waiting carry listed again is
    registered by the pin, since the newer listing has it; folding the held
    carry in as the newer of the two would take it out of the listing instead.
    """
    host_id = HostId.generate()
    services, live, back = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live)
    auto_register = _build_auto_register_carrying_to(
        build_latchkey_auto_register, resolver, latchkey, _FakeMachine(latchkey=latchkey, host_id=host_id)
    )
    held = _PendingCarry(
        address_agent_id=services, listed_agent_ids=frozenset({services, live}), dropped_agent_ids=frozenset({back})
    )
    waiting = _PendingCarry(
        address_agent_id=services, listed_agent_ids=frozenset({services, live, back}), dropped_agent_ids=frozenset()
    )
    with auto_register._push_lock:
        auto_register._hosts_awaiting_machine[host_id] = held
        auto_register._hosts_awaiting_push[host_id] = waiting
        auto_register._hosts_being_pushed.add(host_id)

    auto_register._carry_held(host_id)

    folded = auto_register._hosts_awaiting_push[host_id]
    assert folded.listed_agent_ids == {services, live, back}
    assert folded.dropped_agent_ids == {back}
    assert host_id not in auto_register._hosts_awaiting_machine


def test_reconciles_a_host_file_that_arrives_after_its_listing_was_seen(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A policy adopted from the machine after discovery listed the host carries every agent it ever registered.

    A listing seen while the host had no file was not reconciled against, so
    the file is reconciled against it as soon as it is there, and the dead
    agents it carries go on the next look rather than one later.
    """
    host_id = HostId.generate()
    services, live, gone = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _push_agents(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))
    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()
    assert not permissions_path_for_host(latchkey.plugin_data_dir, host_id).is_file()

    _register_all(latchkey, host_id, services, live, gone)
    resolver.notify_change()
    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live), str(gone)}
    _report_listing(resolver, _make_services_agent(host_id, services), _make_discovered(host_id, live))

    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live)}


def test_leaves_the_registered_agents_of_a_destroyed_host_alone(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
    build_latchkey_auto_register: Callable[..., LatchkeyAutoRegister],
) -> None:
    """A destroyed host lingers in discovery for a while; its policy is nobody's to enforce, so nothing is pushed at it."""
    host_id = HostId.generate()
    services, live, gone = AgentId.generate(), AgentId.generate(), AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _register_all(latchkey, host_id, services, live, gone)
    agents = (_make_services_agent(host_id, services), _make_discovered(host_id, live))
    destroyed = ParsedAgentsResult(
        agent_ids=tuple(agent.agent_id for agent in agents),
        discovered_agents=agents,
        ssh_info_by_agent_id={},
        host_state_by_host_id={str(host_id): HostState.DESTROYED},
    )
    resolver.update_agents(destroyed)

    _build_local_auto_register(build_latchkey_auto_register, resolver, latchkey).start()
    resolver.update_agents(destroyed)
    resolver.notify_change()

    assert _registered_agent_ids(latchkey.plugin_data_dir, host_id) == {str(services), str(live), str(gone)}
