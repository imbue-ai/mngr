"""Unit tests for :mod:`imbue.mngr_latchkey.agent_setup`.

These cover the per-agent latchkey setup helpers without spawning a real
``latchkey gateway`` subprocess. ``Latchkey.start_gateway`` and
the JWT-mint / password-derivation methods are stubbed via a fake
subclass so we can drive the various success / failure permutations
deterministically.
"""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.agent_setup import AgentLatchkeySetup
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_DISABLE_COUNTING
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PASSWORD
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_SECONDARY
from imbue.mngr_latchkey.agent_setup import _build_allowed_agent_anyof_entry
from imbue.mngr_latchkey.agent_setup import _extract_agent_id_from_anyof_entry
from imbue.mngr_latchkey.agent_setup import finalize_host_permissions
from imbue.mngr_latchkey.agent_setup import maybe_recover_host_permissions_for_agent
from imbue.mngr_latchkey.agent_setup import prepare_agent_latchkey
from imbue.mngr_latchkey.agent_setup import register_agent_for_host
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.remote_gateway import INNER_PORT
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import opaque_permissions_dir
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.testing import FakeLatchkey
from imbue.mngr_latchkey.testing import make_full_fake_latchkey


def _full_fake(tmp_path: Path) -> FakeLatchkey:
    return make_full_fake_latchkey(tmp_path)


# -- prepare_agent_latchkey ---------------------------------------------------


def test_prepare_no_latchkey_tunneled_returns_constant_url(tmp_path: Path) -> None:
    """``latchkey=None`` with ``is_tunneled=True`` still injects the constant URL.

    The constant agent-side URL is meaningful even without a configured
    latchkey wrapper -- tests and non-password-protected gateways can
    still receive traffic at that URL.
    """
    setup = prepare_agent_latchkey(None, is_tunneled=True)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    # Tunneled agents also get the secondary (per-VPS) gateway URL on a distinct port.
    assert setup.env[ENV_LATCHKEY_GATEWAY_SECONDARY] == f"http://127.0.0.1:{INNER_PORT}"
    assert INNER_PORT != AGENT_SIDE_LATCHKEY_PORT
    assert setup.env[ENV_LATCHKEY_DISABLE_COUNTING] == "1"
    assert ENV_LATCHKEY_GATEWAY_PASSWORD not in setup.env
    assert ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE not in setup.env
    assert setup.opaque_permissions_path is None


def test_prepare_no_latchkey_on_host_returns_empty(tmp_path: Path) -> None:
    """``latchkey=None`` with ``is_tunneled=False`` cannot produce a URL.

    On-host agents need the gateway's live port; without a Latchkey
    wrapper there is nothing to query.
    """
    setup = prepare_agent_latchkey(None, is_tunneled=False)
    assert setup.env == {}
    assert setup.opaque_permissions_path is None


def test_prepare_full_wiring_tunneled(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
    assert setup.env[ENV_LATCHKEY_GATEWAY_SECONDARY] == f"http://127.0.0.1:{INNER_PORT}"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"
    assert setup.env[ENV_LATCHKEY_DISABLE_COUNTING] == "1"
    assert setup.opaque_permissions_path is not None
    assert setup.opaque_permissions_path.parent == opaque_permissions_dir(fake.plugin_data_dir)
    on_disk = json.loads(setup.opaque_permissions_path.read_text())
    # Two rules now ship in the baseline:
    #
    # 1. ``minds-api-proxy-unauthorized`` (first): scope matches any
    #    ``/minds-api-proxy/api/v1/agents/<id>/...`` request whose <id>
    #    is NOT in the allowed list (encoded as ``not + anyOf`` on the
    #    path schema; initially empty -- no agent allowed). Detent
    #    stops at the first matching scope, and the empty permission
    #    list rejects the request immediately.
    # 2. ``latchkey-self`` (after): the gateway-self endpoints every
    #    agent needs, plus a generic ``minds-api-proxy`` permission
    #    for any path under the proxy's ``/agents/<id>/`` subtree.
    #    Authorized agents (those past Rule 1's ``not + anyOf``) hit
    #    this rule and are let through by the generic permission.
    assert on_disk["rules"] == [
        {"minds-api-proxy-per-agent-unauthorized": []},
        {
            "latchkey-self": [
                "latchkey-self-create-permission-request",
                "latchkey-self-read-self-permissions",
                "latchkey-self-read-available-permissions",
                "minds-api-proxy-per-agent",
            ],
        },
    ]
    schemas = on_disk["schemas"]
    # The minds-api-proxy-unauthorized scope:
    #   * ``domain`` constrained to the gateway-self host,
    #   * ``path`` must match the proxy-prefix pattern AND not match
    #     any agent-id pattern in the (initially empty) ``anyOf`` list.
    unauthorized_scope = schemas["minds-api-proxy-per-agent-unauthorized"]["properties"]
    assert unauthorized_scope["domain"] == {"const": "latchkey-self.invalid"}
    assert unauthorized_scope["path"]["type"] == "string"
    assert unauthorized_scope["path"]["pattern"] == r"^/minds-api-proxy/api/v1/agents/[^/]+(/.*)?$"
    assert unauthorized_scope["path"]["not"] == {"anyOf": []}
    # And the second rule references a generic ``minds-api-proxy``
    # permission whose path matches any ``/agents/<any>/`` request.
    minds_proxy_perm = schemas["minds-api-proxy-per-agent"]["properties"]
    assert minds_proxy_perm["path"]["type"] == "string"
    assert minds_proxy_perm["path"]["pattern"] == r"^/minds-api-proxy/api/v1/agents/[^/]+(/.*)?$"
    # And the gateway-self baseline rule's schemas are unchanged.
    assert schemas["latchkey-self"]["properties"]["domain"] == {"const": "latchkey-self.invalid"}
    assert schemas["latchkey-self-create-permission-request"]["properties"] == {
        "method": {"const": "POST"},
        "path": {"const": "/permission-requests"},
    }
    assert schemas["latchkey-self-read-self-permissions"]["properties"] == {
        "method": {"const": "GET"},
        "path": {"const": "/permissions/self"},
    }
    # The available-catalog permission uses a path pattern so the agent
    # can read any service's entry; it must not be a ``const`` (which
    # would pin it to one service).
    available_path_schema = schemas["latchkey-self-read-available-permissions"]["properties"]["path"]
    assert available_path_schema == {
        "type": "string",
        "pattern": r"^/permissions/available/[a-z0-9][a-z0-9-]*$",
    }


def test_prepare_full_wiring_on_host_uses_live_port(tmp_path: Path) -> None:
    """On-host (DEV) agents get the gateway's live host:port pair."""
    fake = _full_fake(tmp_path)
    with ConcurrencyGroup(name="test-on-host-prepare") as cg:
        setup = prepare_agent_latchkey(fake, is_tunneled=False, concurrency_group=cg)
    assert setup.env[ENV_LATCHKEY_GATEWAY] == "http://127.0.0.1:55555"
    # On-host (DEV) agents run on the gateway host itself -- no per-VPS secondary.
    assert ENV_LATCHKEY_GATEWAY_SECONDARY not in setup.env
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"


def test_prepare_on_host_without_concurrency_group_raises(tmp_path: Path) -> None:
    """is_tunneled=False with a real Latchkey requires a concurrency_group to own the gateway."""
    fake = _full_fake(tmp_path)
    with pytest.raises(LatchkeyError):
        prepare_agent_latchkey(fake, is_tunneled=False)


def test_prepare_on_host_gateway_start_failure_propagates(tmp_path: Path) -> None:
    """Gateway-start failures bubble up to the caller; the helper does not swallow them."""
    fake = FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_error=LatchkeyError("boom"),
        password="hunter2",
        jwt="header.payload.signature",
    )
    with ConcurrencyGroup(name="test-prepare-failure") as cg:
        with pytest.raises(LatchkeyError):
            prepare_agent_latchkey(fake, is_tunneled=False, concurrency_group=cg)


def test_prepare_password_derivation_failure_propagates(tmp_path: Path) -> None:
    """Password-derivation failures bubble up to the caller."""
    fake = FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password_error=LatchkeyJwtMintError("nope"),
        jwt="header.payload.signature",
    )
    with pytest.raises(LatchkeyJwtMintError):
        prepare_agent_latchkey(fake, is_tunneled=True)


def test_prepare_jwt_mint_failure_propagates(tmp_path: Path) -> None:
    """JWT-mint failures bubble up to the caller.

    The opaque permissions file may or may not have been materialized
    at the point the exception fires; we don't make any guarantee
    about cleanup -- the caller can either retry the whole prepare
    (which writes a fresh opaque path) or accept the orphan file. The
    files are tiny and live under the user's own latchkey directory,
    so leaking one occasionally is not a concern.
    """
    fake = FakeLatchkey(latchkey_directory=tmp_path)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password="hunter2",
        jwt_error=LatchkeyJwtMintError("nope"),
    )
    with pytest.raises(LatchkeyJwtMintError):
        prepare_agent_latchkey(fake, is_tunneled=True)


# -- finalize_host_permissions ----------------------------------------------


def test_finalize_links_opaque_to_canonical(tmp_path: Path) -> None:
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.opaque_permissions_path is not None
    host_id = HostId()

    finalize_host_permissions(fake, setup.opaque_permissions_path, host_id)

    # The opaque path is now a symlink pointing at the canonical host path.
    canonical = permissions_path_for_host(fake.plugin_data_dir, host_id)
    assert canonical.is_file()
    assert setup.opaque_permissions_path.is_symlink()
    assert setup.opaque_permissions_path.resolve() == canonical.resolve()


def test_finalize_with_none_path_is_a_noop(tmp_path: Path) -> None:
    """``opaque_permissions_path=None`` is what ``prepare_agent_latchkey`` returns on JWT-mint failure."""
    fake = _full_fake(tmp_path)
    finalize_host_permissions(fake, None, HostId())
    assert not (fake.plugin_data_dir / "hosts").exists()


def test_finalize_propagates_link_errors(tmp_path: Path) -> None:
    """Linking failures bubble up to the caller; the helper does not swallow them.

    Callers (e.g. minds) decide whether to fail agent creation or just
    surface a warning; the plugin's job is just to report what happened.
    """
    fake = _full_fake(tmp_path)
    # Pass an opaque path the helper cannot operate on (it doesn't
    # exist), forcing ``link_opaque_permissions_to_host`` to raise.
    missing_path = tmp_path / "definitely-not-there.json"
    with pytest.raises(LatchkeyStoreError):
        finalize_host_permissions(fake, missing_path, HostId())


# -- maybe_recover_host_permissions_for_agent --------------------------------


def test_recover_links_standalone_opaque_when_host_file_missing(tmp_path: Path) -> None:
    """The common recovery: a standalone opaque baseline handle, no canonical file yet."""
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.opaque_permissions_path is not None
    host_id = HostId()
    agent_id = AgentId()
    canonical = permissions_path_for_host(fake.plugin_data_dir, host_id)
    assert not canonical.exists()

    did_recover = maybe_recover_host_permissions_for_agent(fake, host_id, agent_id, setup.opaque_permissions_path)

    assert did_recover is True
    # The canonical file now exists and the opaque handle is a symlink to it,
    # exactly as a successful ``finalize_host_permissions`` would leave things.
    assert canonical.is_file()
    assert setup.opaque_permissions_path.is_symlink()
    assert setup.opaque_permissions_path.resolve() == canonical.resolve()
    # The requesting agent was registered into the host's allowlist.
    assert str(agent_id) in canonical.read_text()


def test_recover_is_noop_for_file_but_still_registers_agent(tmp_path: Path) -> None:
    """A host that was already finalized needs no file repair, but the agent is still registered.

    Closes the auto-register de-dup gap: an agent first seen while the host
    file was missing is skipped (and de-duped) by discovery-time registration,
    so registering it here on its permission request is the only thing that
    adds it to the allowlist.
    """
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.opaque_permissions_path is not None
    host_id = HostId()
    agent_id = AgentId()
    finalize_host_permissions(fake, setup.opaque_permissions_path, host_id)
    canonical = permissions_path_for_host(fake.plugin_data_dir, host_id)
    assert str(agent_id) not in canonical.read_text()

    did_recover = maybe_recover_host_permissions_for_agent(fake, host_id, agent_id, setup.opaque_permissions_path)

    assert did_recover is False
    assert str(agent_id) in canonical.read_text()


def test_recover_rejects_opaque_path_outside_opaque_directory(tmp_path: Path) -> None:
    """A target outside the plugin's opaque directory is refused (defense-in-depth)."""
    fake = _full_fake(tmp_path)
    stray = tmp_path / "elsewhere" / "permissions.json"
    stray.parent.mkdir(parents=True)
    stray.write_text("{}")
    with pytest.raises(LatchkeyStoreError):
        maybe_recover_host_permissions_for_agent(fake, HostId(), AgentId(), stray)


def test_recover_materializes_baseline_when_opaque_handle_missing(tmp_path: Path) -> None:
    """Defensive branch: handle gone but a valid opaque-dir path -> write baseline at canonical."""
    fake = _full_fake(tmp_path)
    host_id = HostId()
    agent_id = AgentId()
    # A path under the opaque directory that was never materialized.
    phantom = opaque_permissions_dir(fake.plugin_data_dir) / "deadbeefdeadbeefdeadbeefdeadbeef.json"
    canonical = permissions_path_for_host(fake.plugin_data_dir, host_id)
    assert not canonical.exists()

    did_recover = maybe_recover_host_permissions_for_agent(fake, host_id, agent_id, phantom)

    assert did_recover is True
    assert canonical.is_file()
    # The baseline carries the gateway-self + minds-api-proxy scaffolding rules.
    config = json.loads(canonical.read_text())
    assert len(config["rules"]) > 0
    assert str(agent_id) in canonical.read_text()
    # The missing opaque handle was (re)created as a symlink to the canonical
    # file, so the agent's JWT (which resolves to the handle) works again.
    assert phantom.is_symlink()
    assert phantom.resolve() == canonical.resolve()


# -- AgentLatchkeySetup model -------------------------------------------------


def test_agent_latchkey_setup_default_opaque_path_is_none() -> None:
    setup = AgentLatchkeySetup(env={})
    assert setup.opaque_permissions_path is None
    assert isinstance(setup.env, Mapping)


# -- Allowed-agent anyOf helpers ---------------------------------------------


def test_allowed_agent_anyof_round_trip() -> None:
    """Each agent id can be built into an ``anyOf`` entry and recovered back."""
    for agent_id in (
        "agent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "agent-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "agent-cccccccccccccccccccccccccccccccc",
    ):
        entry = _build_allowed_agent_anyof_entry(agent_id)
        assert _extract_agent_id_from_anyof_entry(entry) == agent_id


def test_build_allowed_agent_anyof_entry_rejects_unsafe_agent_id() -> None:
    """Agent ids that contain regex metacharacters are rejected at build time.

    We embed the id verbatim into a regex pattern, so any character
    outside ``[A-Za-z0-9_-]`` would either change the regex's meaning
    or break the symmetric extractor. ``LatchkeyStoreError`` raised
    here is preferable to a silently-malformed pattern on disk.
    """
    with pytest.raises(LatchkeyStoreError):
        _build_allowed_agent_anyof_entry("agent-with.dot")


def test_extract_agent_id_from_anyof_entry_raises_on_unrecognized_entry() -> None:
    """An ``anyOf`` entry whose shape doesn't match what we write must raise.

    Otherwise a hand-edited permissions file would get silently rebuilt
    on the next ``register_agent_for_host`` call, discarding the operator's
    edit.
    """
    with pytest.raises(LatchkeyStoreError):
        _extract_agent_id_from_anyof_entry({"pattern": "^/totally/different/shape$"})
    with pytest.raises(LatchkeyStoreError):
        _extract_agent_id_from_anyof_entry({"const": "not-a-pattern-entry"})
    with pytest.raises(LatchkeyStoreError):
        _extract_agent_id_from_anyof_entry("not-even-a-dict")


# -- register_agent_for_host -------------------------------------------------


def _allowed_anyof_for_host(tmp_path: Path, host_id: HostId) -> list[dict[str, str]]:
    """Return the parsed ``anyOf`` list from the host's permissions file."""
    path = permissions_path_for_host(tmp_path, host_id)
    config = json.loads(path.read_text())
    return config["schemas"]["minds-api-proxy-per-agent-unauthorized"]["properties"]["path"]["not"]["anyOf"]


def test_register_agent_for_host_creates_baseline_when_file_absent(tmp_path: Path) -> None:
    """First call for a host writes the baseline + adds the agent."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    register_agent_for_host(tmp_path, host_id, agent_id)
    any_of = _allowed_anyof_for_host(tmp_path, host_id)
    assert len(any_of) == 1
    assert _extract_agent_id_from_anyof_entry(any_of[0]) == str(agent_id)


def test_register_agent_for_host_is_idempotent(tmp_path: Path) -> None:
    """Re-registering an already-registered agent is a no-op."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    register_agent_for_host(tmp_path, host_id, agent_id)
    register_agent_for_host(tmp_path, host_id, agent_id)
    any_of = _allowed_anyof_for_host(tmp_path, host_id)
    assert len(any_of) == 1


def test_register_agent_for_host_accumulates_across_agents(tmp_path: Path) -> None:
    """Multiple agents on the same host all end up in the allowed ``anyOf`` list."""
    host_id = HostId.generate()
    agent_a = AgentId.generate()
    agent_b = AgentId.generate()
    agent_c = AgentId.generate()
    register_agent_for_host(tmp_path, host_id, agent_a)
    register_agent_for_host(tmp_path, host_id, agent_b)
    register_agent_for_host(tmp_path, host_id, agent_c)
    any_of = _allowed_anyof_for_host(tmp_path, host_id)
    parsed = {_extract_agent_id_from_anyof_entry(e) for e in any_of}
    assert parsed == {str(agent_a), str(agent_b), str(agent_c)}


def test_register_agent_for_host_preserves_other_grants(tmp_path: Path) -> None:
    """Registering a new agent does not disturb the baseline rules or other schemas."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    register_agent_for_host(tmp_path, host_id, agent_id)
    path = permissions_path_for_host(tmp_path, host_id)
    config = json.loads(path.read_text())
    rule_keys = [next(iter(rule.keys())) for rule in config["rules"]]
    # The minds-api-proxy-unauthorized rule must come first so detent
    # stops at it for an unauthorized agent_id (rejecting with the
    # empty permission list) rather than falling through to the
    # latchkey-self baseline rule.
    assert rule_keys == ["minds-api-proxy-per-agent-unauthorized", "latchkey-self"]


def test_register_agent_for_host_raises_when_anyof_was_hand_edited(tmp_path: Path) -> None:
    """A corrupted / hand-edited permissions file is not silently overwritten."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    # Bootstrap with the baseline + one agent, then replace one of the
    # ``anyOf`` entries with a pattern the parser will reject.
    register_agent_for_host(tmp_path, host_id, AgentId.generate())
    path = permissions_path_for_host(tmp_path, host_id)
    config = json.loads(path.read_text())
    config["schemas"]["minds-api-proxy-per-agent-unauthorized"]["properties"]["path"]["not"]["anyOf"] = [
        {"pattern": "^/no-longer-recognized$"}
    ]
    path.write_text(json.dumps(config))
    with pytest.raises(LatchkeyStoreError):
        register_agent_for_host(tmp_path, host_id, agent_id)
