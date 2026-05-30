"""Unit tests for :mod:`imbue.minds.desktop_client.latchkey_auto_register`."""

import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.latchkey_auto_register import LatchkeyAutoRegister
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
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


@pytest.fixture
def resolver() -> MngrCliBackendResolver:
    return MngrCliBackendResolver()


def test_registers_existing_agents_on_start(tmp_path: Path, resolver: MngrCliBackendResolver) -> None:
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

    LatchkeyAutoRegister(backend_resolver=resolver, latchkey=latchkey).start()

    any_of = _read_allowed_anyof(latchkey.plugin_data_dir, host_id)
    registered = {entry["pattern"] for entry in any_of}
    assert any(str(new_agent) in p for p in registered)
    assert any(str(seed_agent) in p for p in registered)


def test_registers_newly_discovered_agents_on_change(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
) -> None:
    """Agents that appear in later discovery ticks get registered without a restart."""
    host_id = HostId.generate()
    seed_agent = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    register_agent_for_host(latchkey.plugin_data_dir, host_id, seed_agent)

    LatchkeyAutoRegister(backend_resolver=resolver, latchkey=latchkey).start()

    later_agent = AgentId.generate()
    _push_agents(
        resolver,
        _make_discovered(host_id, seed_agent),
        _make_discovered(host_id, later_agent),
    )

    any_of = _read_allowed_anyof(latchkey.plugin_data_dir, host_id)
    registered = {entry["pattern"] for entry in any_of}
    assert any(str(later_agent) in p for p in registered)


def test_skips_hosts_without_permissions_file(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
) -> None:
    """Hosts that have no existing permissions file are intentionally skipped.

    The file is materialized at host-creation time by
    :func:`finalize_host_permissions`; its absence means the host is not
    minds-managed and we must not conjure one from a discovery event alone.
    """
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    latchkey = make_full_fake_latchkey(tmp_path)
    _push_agents(resolver, _make_discovered(host_id, agent_id))

    LatchkeyAutoRegister(backend_resolver=resolver, latchkey=latchkey).start()

    assert not permissions_path_for_host(latchkey.plugin_data_dir, host_id).exists()


def test_idempotent_across_repeated_discovery_ticks(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
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

    auto = LatchkeyAutoRegister(backend_resolver=resolver, latchkey=latchkey)
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

    LatchkeyAutoRegister(backend_resolver=resolver, latchkey=latchkey).start()

    a_patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_a)}
    b_patterns = {e["pattern"] for e in _read_allowed_anyof(latchkey.plugin_data_dir, host_b)}
    assert any(str(agent_a) in p for p in a_patterns)
    assert not any(str(agent_a) in p for p in b_patterns)
    assert any(str(agent_b) in p for p in b_patterns)
    assert not any(str(agent_b) in p for p in a_patterns)


def test_corrupted_permissions_file_logs_but_does_not_retry_forever(
    tmp_path: Path,
    resolver: MngrCliBackendResolver,
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

    auto = LatchkeyAutoRegister(backend_resolver=resolver, latchkey=latchkey)
    # ``start()`` must not raise even though ``register_agent_for_host``
    # raises ``LatchkeyStoreError`` against the corrupted file.
    auto.start()

    # Fire another change: the pair is in the dedup set, so no new
    # write attempt happens. We assert via reading the file -- it
    # should be unchanged from the corrupted state.
    _push_agents(resolver, _make_discovered(host_id, agent_id))
    assert json.loads(perms_path.read_text()) == config
