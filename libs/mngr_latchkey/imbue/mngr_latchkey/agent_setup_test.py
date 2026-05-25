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
from imbue.mngr_latchkey.agent_setup import _build_allowed_agent_path_pattern
from imbue.mngr_latchkey.agent_setup import _parse_allowed_agent_path_pattern
from imbue.mngr_latchkey.agent_setup import allow_agent_for_host
from imbue.mngr_latchkey.agent_setup import finalize_host_permissions
from imbue.mngr_latchkey.agent_setup import prepare_agent_latchkey
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
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
    assert setup.env[ENV_LATCHKEY_GATEWAY_PASSWORD] == "hunter2"
    assert setup.env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE] == "header.payload.signature"
    assert setup.env[ENV_LATCHKEY_DISABLE_COUNTING] == "1"
    assert setup.opaque_permissions_path is not None
    assert setup.opaque_permissions_path.parent == opaque_permissions_dir(fake.plugin_data_dir)
    on_disk = json.loads(setup.opaque_permissions_path.read_text())
    # Two rules now ship in the baseline:
    #
    # 1. ``minds-api-proxy`` (first): matches any
    #    ``/minds-api-proxy/api/v1/agents/<id>/...`` request and
    #    constrains <id> to the empty allowed-agent enum -- detent
    #    stops at the first matching scope and rejects, so an
    #    unauthorized agent_id never falls through to the
    #    gateway-self rule below.
    # 2. ``latchkey-self`` (after): the three gateway-self endpoints
    #    every agent needs (create permission request, read own
    #    permissions, read services catalog).
    assert on_disk["rules"] == [
        {"minds-api-proxy": ["minds-api-proxy-allowed-agent"]},
        {
            "latchkey-self": [
                "latchkey-self-create-permission-request",
                "latchkey-self-read-self-permissions",
                "latchkey-self-read-available-permissions",
            ],
        },
    ]
    schemas = on_disk["schemas"]
    # The minds-api-proxy scope matches every agents/<id>/... request
    # under the gateway-self domain.
    minds_proxy_scope = schemas["minds-api-proxy"]["properties"]
    assert minds_proxy_scope["domain"] == {"const": "latchkey-self.invalid"}
    assert minds_proxy_scope["path"]["type"] == "string"
    assert minds_proxy_scope["path"]["pattern"] == r"^/minds-api-proxy/api/v1/agents/[^/]+(/.*)?$"
    # The allowed-agent permission starts with an empty enum (the
    # ``(?!)`` negative-empty-lookahead sentinel) -- no agent_id is
    # allowed until ``mngr latchkey allow-agent`` appends to it.
    allowed_agent_pattern = schemas["minds-api-proxy-allowed-agent"]["properties"]["path"]["pattern"]
    assert allowed_agent_pattern == r"^/minds-api-proxy/api/v1/agents/(?:(?!))(/.*)?$"
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


# -- AgentLatchkeySetup model -------------------------------------------------


def test_agent_latchkey_setup_default_opaque_path_is_none() -> None:
    setup = AgentLatchkeySetup(env={})
    assert setup.opaque_permissions_path is None
    assert isinstance(setup.env, Mapping)


# -- Allowed-agent path pattern helpers --------------------------------------


def test_allowed_agent_path_pattern_round_trip_empty() -> None:
    assert _parse_allowed_agent_path_pattern(_build_allowed_agent_path_pattern(())) == ()


def test_allowed_agent_path_pattern_round_trip_single() -> None:
    ids = ("agent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",)
    assert _parse_allowed_agent_path_pattern(_build_allowed_agent_path_pattern(ids)) == ids


def test_allowed_agent_path_pattern_round_trip_multiple() -> None:
    ids = (
        "agent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "agent-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "agent-cccccccccccccccccccccccccccccccc",
    )
    assert _parse_allowed_agent_path_pattern(_build_allowed_agent_path_pattern(ids)) == ids


def test_parse_allowed_agent_path_pattern_raises_on_hand_edited_pattern() -> None:
    """An unrecognized pattern must raise rather than silently rebuild from scratch.

    Otherwise a hand-edited permissions file would get its custom
    pattern overwritten on the next ``allow_agent_for_host`` call.
    """
    with pytest.raises(LatchkeyStoreError):
        _parse_allowed_agent_path_pattern("^/totally/not/the/expected/shape$")


# -- allow_agent_for_host ----------------------------------------------------


def test_allow_agent_for_host_creates_baseline_when_file_absent(tmp_path: Path) -> None:
    """First call for a host writes the baseline + adds the agent."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    allow_agent_for_host(tmp_path, host_id, agent_id)
    path = permissions_path_for_host(tmp_path, host_id)
    assert path.is_file()
    config_text = path.read_text()
    assert str(agent_id) in config_text
    # The baseline rule structure must be present (first rule = minds
    # api proxy, second = latchkey-self).
    assert "minds-api-proxy" in config_text
    assert "latchkey-self" in config_text


def test_allow_agent_for_host_is_idempotent(tmp_path: Path) -> None:
    """Re-allowing an already-allowed agent is a no-op."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    allow_agent_for_host(tmp_path, host_id, agent_id)
    allow_agent_for_host(tmp_path, host_id, agent_id)
    path = permissions_path_for_host(tmp_path, host_id)
    # The agent id appears exactly once in the path pattern.
    config_text = path.read_text()
    assert config_text.count(str(agent_id)) == 1


def test_allow_agent_for_host_accumulates_across_agents(tmp_path: Path) -> None:
    """Multiple agents on the same host are all listed in the allowed enum."""
    host_id = HostId.generate()
    agent_a = AgentId.generate()
    agent_b = AgentId.generate()
    agent_c = AgentId.generate()
    allow_agent_for_host(tmp_path, host_id, agent_a)
    allow_agent_for_host(tmp_path, host_id, agent_b)
    allow_agent_for_host(tmp_path, host_id, agent_c)
    path = permissions_path_for_host(tmp_path, host_id)
    config = json.loads(path.read_text())
    pattern = config["schemas"]["minds-api-proxy-allowed-agent"]["properties"]["path"]["pattern"]
    parsed = _parse_allowed_agent_path_pattern(pattern)
    assert set(parsed) == {str(agent_a), str(agent_b), str(agent_c)}


def test_allow_agent_for_host_preserves_other_grants(tmp_path: Path) -> None:
    """Allowing a new agent does not disturb the baseline rules or other schemas."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    allow_agent_for_host(tmp_path, host_id, agent_id)
    path = permissions_path_for_host(tmp_path, host_id)
    config = json.loads(path.read_text())
    rule_keys = [next(iter(rule.keys())) for rule in config["rules"]]
    # The minds-api-proxy rule must come first so detent stops at it
    # for an unauthorized agent_id rather than falling through.
    assert rule_keys == ["minds-api-proxy", "latchkey-self"]


def test_allow_agent_for_host_raises_when_pattern_was_hand_edited(tmp_path: Path) -> None:
    """A corrupted / hand-edited permissions file is not silently overwritten."""
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    # Bootstrap with the baseline, then mangle the path pattern.
    allow_agent_for_host(tmp_path, host_id, AgentId.generate())
    path = permissions_path_for_host(tmp_path, host_id)
    config = json.loads(path.read_text())
    config["schemas"]["minds-api-proxy-allowed-agent"]["properties"]["path"]["pattern"] = "^/no-longer-recognized$"
    path.write_text(json.dumps(config))
    with pytest.raises(LatchkeyStoreError):
        allow_agent_for_host(tmp_path, host_id, agent_id)
