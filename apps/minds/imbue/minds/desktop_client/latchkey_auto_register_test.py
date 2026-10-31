"""Unit tests for :mod:`imbue.minds.desktop_client.latchkey_auto_register`."""

import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

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
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
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


def _provision_machine(latchkey: Latchkey, host_id: HostId) -> None:
    """Record a machine key for ``host_id``, as the provisioning pass does once the host's gateway is wired."""
    store_machine_encryption_key(latchkey.plugin_data_dir, host_id, SecretStr("machine-key-7c1d"))


def _build_local_auto_register(
    build: Callable[..., LatchkeyAutoRegister], resolver: MngrCliBackendResolver, latchkey: Latchkey
) -> LatchkeyAutoRegister:
    """An auto-register whose hosts have no machine of their own, for tests about the local edit."""
    return build(
        backend_resolver=resolver, latchkey=latchkey, push_permissions_to_machine=leave_permissions_on_this_computer
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
    _push_agents(resolver, _make_discovered(host_id, new_agent))
    recorder = _PushRecorder()

    build_latchkey_auto_register(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
    ).start()

    _wait_for_pushes(recorder, 1)
    assert recorder.pushed_workspace_agent_ids == [str(new_agent)]


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
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    recorder = _PushRecorder()

    auto = build_latchkey_auto_register(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
    )
    auto.start()
    resolver.notify_change()

    # The registration is a synchronous no-op, so no push thread was ever
    # started: nothing to wait for, and the recorder must still be empty.
    assert recorder.pushed_workspace_agent_ids == []


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
    _push_agents(resolver, _make_discovered(host_id, first_agent))
    recorder = _PushRecorder(failing_workspace_agent_ids=frozenset({str(first_agent)}))

    build_latchkey_auto_register(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
    ).start()
    _wait_for_pushes(recorder, 1)

    _push_agents(resolver, _make_discovered(host_id, first_agent), _make_discovered(host_id, second_agent))

    _wait_for_pushes(recorder, 2)
    assert recorder.pushed_workspace_agent_ids == [str(first_agent), str(second_agent)]


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
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    recorder = _PushRecorder()

    auto_register = build_latchkey_auto_register(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
        machine_poll_interval_seconds=0.05,
    )
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    # A resolver change before the machine exists carries nothing, and does not drop the push either.
    resolver.notify_change()
    _wait_until_pushes_settle(auto_register)
    assert recorder.pushed_workspace_agent_ids == []
    assert host_id in auto_register._hosts_awaiting_machine

    # No resolver change follows the key: the watch alone carries the push.
    _provision_machine(latchkey, host_id)

    _wait_for_pushes(recorder, 1)
    _wait_until_pushes_settle(auto_register)
    assert recorder.pushed_workspace_agent_ids == [str(agent_id)]
    # The held push is consumed: later changes do not push it again.
    resolver.notify_change()
    _wait_until_pushes_settle(auto_register)
    assert recorder.pushed_workspace_agent_ids == [str(agent_id)]
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
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    recorder = _PushRecorder()

    auto_register = build_latchkey_auto_register(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
        machine_poll_interval_seconds=0.01,
        machine_watch_seconds=0.0,
    )
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    _wait_until_machine_watch_has_ended(auto_register, host_id)

    _provision_machine(latchkey, host_id)
    _wait_until_pushes_settle(auto_register)
    assert recorder.pushed_workspace_agent_ids == []

    resolver.notify_change()
    _wait_for_pushes(recorder, 1)
    _wait_until_pushes_settle(auto_register)
    assert recorder.pushed_workspace_agent_ids == [str(agent_id)]
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
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    recorder = _PushRecorder()

    auto_register = build_latchkey_auto_register(
        backend_resolver=resolver,
        latchkey=latchkey,
        push_permissions_to_machine=recorder.push,
        machine_poll_interval_seconds=0.01,
        machine_watch_seconds=0.1,
    )
    auto_register.start()
    _wait_until_push_is_held(auto_register, host_id)
    _wait_until_machine_watch_has_ended(auto_register, host_id)
    resolver.notify_change()
    resolver.notify_change()
    _wait_until_pushes_settle(auto_register)

    assert recorder.pushed_workspace_agent_ids == []
    assert host_id in auto_register._hosts_awaiting_machine


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
    recorder = _PushRecorder()
    auto_register = build_latchkey_auto_register(
        backend_resolver=resolver, latchkey=latchkey, push_permissions_to_machine=recorder.push
    )
    auto_register.start()
    root_concurrency_group.shutdown()

    agent_id = AgentId.generate()
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    assert recorder.pushed_workspace_agent_ids == []
    assert auto_register._hosts_awaiting_push == {host_id: agent_id}
    assert host_id not in auto_register._hosts_being_pushed
