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
from typing import Any

import jsonschema
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.agent_setup import AgentLatchkeySetup
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_DISABLE_COUNTING
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PASSWORD
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
from imbue.mngr_latchkey.agent_setup import ensure_minds_schema_in_existing_host_files
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
    # Every new agent gets three baseline permissions under the
    # ``latchkey-self`` scope: create a permission request, read its own
    # current permissions, and read the per-service permissions catalog.
    assert on_disk["rules"] == [
        {
            "latchkey-self": [
                "latchkey-self-create-permission-request",
                "latchkey-self-read-self-permissions",
                "latchkey-self-read-available-permissions",
            ],
        },
    ]
    schemas = on_disk["schemas"]
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
    # The ``minds`` scope and its named permissions are materialized
    # inline but NOT pre-granted via a rule; agents must go through the
    # standard permission-request dialog before their first spawn.
    assert schemas["minds"] == {
        "properties": {
            "domain": {"const": "127.0.0.1"},
            "path": {"type": "string", "pattern": r"^/api/create-agent(/|$)"},
        },
        "required": ["domain", "path"],
    }
    assert schemas["minds-create"] == {
        "properties": {
            "method": {"const": "POST"},
            "path": {"const": "/api/create-agent"},
        },
        "required": ["method", "path"],
    }
    assert schemas["minds-status"] == {
        "properties": {
            "method": {"const": "GET"},
            "path": {
                "type": "string",
                "pattern": r"^/api/create-agent/creation-[0-9a-f]{32}/status$",
            },
        },
        "required": ["method", "path"],
    }
    assert schemas["minds-logs"] == {
        "properties": {
            "method": {"const": "GET"},
            "path": {
                "type": "string",
                "pattern": r"^/api/create-agent/creation-[0-9a-f]{32}/logs$",
            },
        },
        "required": ["method", "path"],
    }
    granted_scopes = {key for rule in on_disk["rules"] for key in rule}
    assert "minds" not in granted_scopes


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


# -- ensure_minds_schema_in_existing_host_files ---------------------

_MINDS_SCHEMA_KEYS_EXPECTED: tuple[str, ...] = (
    "minds",
    "minds-create",
    "minds-status",
    "minds-logs",
)


def _write_host_permissions(host_dir: Path, payload: dict[str, Any]) -> Path:
    host_dir.mkdir(parents=True, exist_ok=True)
    path = host_dir / "latchkey_permissions.json"
    path.write_text(json.dumps(payload, indent=2))
    return path


def test_migration_injects_schema_into_existing_file(tmp_path: Path) -> None:
    """A pre-existing per-host file without the schemas gets them added; rules left untouched."""
    plugin_data_dir = tmp_path / "mngr_latchkey"
    host_dir = plugin_data_dir / "hosts" / "host-abc"
    existing_rule = {"slack-api": ["slack-read-all"]}
    path = _write_host_permissions(host_dir, {"rules": [existing_rule], "schemas": {}})

    migrated = ensure_minds_schema_in_existing_host_files(plugin_data_dir)

    assert migrated == 1
    on_disk = json.loads(path.read_text())
    assert on_disk["rules"] == [existing_rule]
    for key in _MINDS_SCHEMA_KEYS_EXPECTED:
        assert key in on_disk["schemas"]
    assert on_disk["schemas"]["minds-create"]["properties"]["path"] == {"const": "/api/create-agent"}
    assert on_disk["schemas"]["minds-status"]["properties"]["method"] == {"const": "GET"}


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Files that already have the up-to-date schemas are not rewritten."""
    plugin_data_dir = tmp_path / "mngr_latchkey"
    host_dir = plugin_data_dir / "hosts" / "host-abc"
    # Seed with the schemas the migration would install, by running it
    # once against an empty schemas block; subsequent runs must be no-ops.
    _write_host_permissions(host_dir, {"rules": [], "schemas": {}})

    assert ensure_minds_schema_in_existing_host_files(plugin_data_dir) == 1
    # Second run is a no-op.
    assert ensure_minds_schema_in_existing_host_files(plugin_data_dir) == 0
    # Third run is also a no-op.
    assert ensure_minds_schema_in_existing_host_files(plugin_data_dir) == 0


def test_migration_skips_other_unrelated_schemas(tmp_path: Path) -> None:
    """Other schemas in the file (e.g. an old custom one) survive untouched."""
    plugin_data_dir = tmp_path / "mngr_latchkey"
    host_dir = plugin_data_dir / "hosts" / "host-abc"
    other_schema = {"properties": {"domain": {"const": "example.invalid"}}, "required": ["domain"]}
    path = _write_host_permissions(host_dir, {"rules": [], "schemas": {"custom": other_schema}})

    ensure_minds_schema_in_existing_host_files(plugin_data_dir)

    on_disk = json.loads(path.read_text())
    assert on_disk["schemas"]["custom"] == other_schema
    for key in _MINDS_SCHEMA_KEYS_EXPECTED:
        assert key in on_disk["schemas"]


def test_migration_returns_zero_when_no_hosts_dir(tmp_path: Path) -> None:
    plugin_data_dir = tmp_path / "mngr_latchkey"
    assert ensure_minds_schema_in_existing_host_files(plugin_data_dir) == 0


def test_migration_skips_malformed_files(tmp_path: Path) -> None:
    """A garbage file in a host dir does not crash the migration; other host files still get migrated."""
    plugin_data_dir = tmp_path / "mngr_latchkey"
    bad_dir = plugin_data_dir / "hosts" / "host-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "latchkey_permissions.json").write_text("not valid json {{{")
    good_dir = plugin_data_dir / "hosts" / "host-good"
    good_path = _write_host_permissions(good_dir, {"rules": [], "schemas": {}})

    migrated = ensure_minds_schema_in_existing_host_files(plugin_data_dir)
    assert migrated == 1
    on_disk_schemas = json.loads(good_path.read_text())["schemas"]
    for key in _MINDS_SCHEMA_KEYS_EXPECTED:
        assert key in on_disk_schemas


def test_migration_rewrites_when_only_subset_of_keys_present(tmp_path: Path) -> None:
    """A file with the scope schema but missing one named permission gets rewritten."""
    plugin_data_dir = tmp_path / "mngr_latchkey"
    host_dir = plugin_data_dir / "hosts" / "host-abc"
    # Seed with all keys present so we can simulate a partial state.
    _write_host_permissions(host_dir, {"rules": [], "schemas": {}})
    assert ensure_minds_schema_in_existing_host_files(plugin_data_dir) == 1

    path = host_dir / "latchkey_permissions.json"
    on_disk = json.loads(path.read_text())
    # Remove just one of the named-permission schemas; migration must
    # re-install it (and only rewrite when something is missing).
    del on_disk["schemas"]["minds-logs"]
    path.write_text(json.dumps(on_disk, indent=2))

    migrated = ensure_minds_schema_in_existing_host_files(plugin_data_dir)
    assert migrated == 1
    on_disk_schemas = json.loads(path.read_text())["schemas"]
    assert "minds-logs" in on_disk_schemas


def test_schema_admits_intended_requests_and_rejects_others(tmp_path: Path) -> None:
    """Validate the minds schemas against a representative request matrix.

    Uses ``jsonschema`` directly: the gateway's detent evaluator is a JSON
    Schema validator under the hood, so checking each schema against a
    request envelope is a faithful proxy for end-to-end matching.
    """
    fake = _full_fake(tmp_path)
    setup = prepare_agent_latchkey(fake, is_tunneled=True)
    assert setup.opaque_permissions_path is not None
    schemas = json.loads(setup.opaque_permissions_path.read_text())["schemas"]
    scope = schemas["minds"]
    create = schemas["minds-create"]
    status = schemas["minds-status"]
    logs = schemas["minds-logs"]

    creation_id = "creation-" + "0" * 32

    def admits(schema: dict[str, Any], envelope: dict[str, Any]) -> bool:
        try:
            jsonschema.validate(envelope, schema)
            return True
        except jsonschema.ValidationError:
            return False

    base = {"domain": "127.0.0.1"}

    # Scope schema admits any minds-management path under the prefix.
    assert admits(scope, {**base, "path": "/api/create-agent"})
    assert admits(scope, {**base, "path": f"/api/create-agent/{creation_id}/status"})
    assert admits(scope, {**base, "path": f"/api/create-agent/{creation_id}/logs"})
    # Scope schema rejects anything off-prefix or off-domain.
    assert not admits(scope, {**base, "path": "/api/destroy-agent/x"})
    assert not admits(scope, {**base, "path": "/anything-else"})
    assert not admits(scope, {"domain": "example.com", "path": "/api/create-agent"})

    # Named permissions admit exactly their (method, path).
    assert admits(create, {"method": "POST", "path": "/api/create-agent"})
    assert not admits(create, {"method": "GET", "path": "/api/create-agent"})
    assert not admits(create, {"method": "POST", "path": f"/api/create-agent/{creation_id}/status"})

    assert admits(status, {"method": "GET", "path": f"/api/create-agent/{creation_id}/status"})
    assert not admits(status, {"method": "POST", "path": f"/api/create-agent/{creation_id}/status"})
    assert not admits(status, {"method": "GET", "path": "/api/create-agent/not-a-real-id/status"})
    assert not admits(status, {"method": "GET", "path": f"/api/create-agent/{creation_id}/logs"})

    assert admits(logs, {"method": "GET", "path": f"/api/create-agent/{creation_id}/logs"})
    assert not admits(logs, {"method": "POST", "path": f"/api/create-agent/{creation_id}/logs"})
    assert not admits(logs, {"method": "GET", "path": f"/api/create-agent/{creation_id}/status"})
