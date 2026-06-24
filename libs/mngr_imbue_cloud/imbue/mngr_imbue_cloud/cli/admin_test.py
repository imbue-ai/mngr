"""Tests for ``mngr imbue_cloud admin pool ...`` provider-generic helpers.

We don't exercise the full subprocess pipeline (that needs a real OVH
provider + a real Neon DB); instead we cover the small pure helpers
that encode the contract this command commits to: tag-value validation,
ufw command ordering, and the CLI signature.
"""

from typing import Any

import click
import pytest
from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.admin import PoolHostUnderlyingTeardown
from imbue.mngr_imbue_cloud.cli.admin import _CONTAINER_SSH_PORT
from imbue.mngr_imbue_cloud.cli.admin import _INSERT_POOL_HOST_SQL
from imbue.mngr_imbue_cloud.cli.admin import _POOL_HOST_LIST_COLUMNS
from imbue.mngr_imbue_cloud.cli.admin import _ufw_provision_commands
from imbue.mngr_imbue_cloud.cli.admin import build_extra_tags_env_value
from imbue.mngr_imbue_cloud.cli.admin import build_pool_host_insert_values
from imbue.mngr_imbue_cloud.cli.admin import pool
from imbue.mngr_imbue_cloud.cli.admin import resolve_underlying_teardown


def test_build_extra_tags_env_value_empty() -> None:
    assert build_extra_tags_env_value(()) == ""


def test_build_extra_tags_env_value_single_entry() -> None:
    assert build_extra_tags_env_value(("minds_env=alice",)) == "minds_env=alice"


def test_build_extra_tags_env_value_multiple_entries_join_with_comma() -> None:
    assert build_extra_tags_env_value(("minds_env=alice", "pool-owner=bob")) == "minds_env=alice,pool-owner=bob"


def test_build_extra_tags_env_value_rejects_entry_without_equals() -> None:
    with pytest.raises(click.UsageError, match="KEY=VALUE"):
        build_extra_tags_env_value(("minds_env=alice", "no-equals"))


def test_ufw_provision_commands_allows_port_22_before_enable() -> None:
    """ufw enable must come *after* allow 22, otherwise the in-progress SSH session is killed."""
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    allow_22_index = next(i for i, c in enumerate(commands) if c == "ufw allow 22/tcp")
    enable_index = next(i for i, c in enumerate(commands) if c == "ufw --force enable")
    assert allow_22_index < enable_index


def test_ufw_provision_commands_allows_container_ssh_port() -> None:
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    assert f"ufw allow {_CONTAINER_SSH_PORT}/tcp" in commands


def test_ufw_provision_commands_installs_ufw_first() -> None:
    """ufw must be installed before any ufw allow / enable can run."""
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    install_index = next(i for i, c in enumerate(commands) if "install -y ufw" in c)
    first_ufw_use = next(i for i, c in enumerate(commands) if c.startswith("ufw "))
    assert install_index < first_ufw_use


def test_ufw_provision_commands_sets_default_deny_incoming() -> None:
    commands = _ufw_provision_commands(_CONTAINER_SSH_PORT)
    assert "ufw default deny incoming" in commands
    assert "ufw default allow outgoing" in commands


def test_pool_create_requires_region() -> None:
    runner = CliRunner()
    result = runner.invoke(
        pool,
        [
            "create",
            "--count",
            "1",
            "--attributes",
            "{}",
            "--workspace-dir",
            ".",
            "--management-public-key-file",
            "/dev/null",
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "Missing option" in result.output and "--region" in result.output


def test_pool_create_rejects_ovh_datacenter_code_as_region() -> None:
    """A raw OVH datacenter code (e.g. 'vin') must be rejected -- only lease labels are valid.

    Regression test: feeding the datacenter code that `admin server list` prints
    (``vin``) instead of the lease-region label (``US-EAST-VA``) stamps an
    unleasable region onto every baked pool_hosts row, since the connector's
    region filter is an exact, never-relaxed string match.
    """
    runner = CliRunner()
    result = runner.invoke(
        pool,
        [
            "create",
            "--count",
            "1",
            "--backend",
            "slice",
            "--region",
            "vin",
            "--server-id",
            "00000000-0000-0000-0000-000000000000",
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "not a known lease region" in result.output
    assert "US-EAST-VA" in result.output


def test_pool_hosts_insert_has_required_columns() -> None:
    """The INSERT must include every pool_hosts NOT-NULL column the schema requires.

    Regression test for the 2026-05 ``host_name`` drift: schema migration
    added ``host_name NOT NULL`` but the bake's INSERT never picked it up,
    so every successful VPS provision ended in a stranded VPS + a 500 on
    the final DB write.

    Asserting the literal column list keeps this test cheap (no fake DB)
    while still catching any future drop of a required column.
    """
    required_columns = (
        "id",
        "vps_address",
        "vps_instance_id",
        "agent_id",
        "host_id",
        "host_name",
        "ssh_port",
        "ssh_user",
        "container_ssh_port",
        "status",
        "attributes",
        "created_at",
    )
    for column in required_columns:
        assert column in _INSERT_POOL_HOST_SQL, (
            f"Pool host INSERT is missing required column {column!r}; this is the same drift "
            f"class as the host_name regression. SQL: {_INSERT_POOL_HOST_SQL!r}"
        )


def test_pool_list_covers_every_pool_host_column() -> None:
    """`pool list` must surface every pool_hosts column, not a hand-maintained subset.

    Regression test for the drift where region, backend_kind, and the slice
    identifiers (bare_metal_server_id / lima_instance_name / lima_disk_name) were
    absent from the list output -- so a baked slice looked like a region-less OVH
    VPS. Because `_POOL_HOST_LIST_COLUMNS` now drives both the SELECT and the
    emitted JSON keys, asserting it equals the full schema keeps the two in
    lockstep and forces any new pool_hosts migration column to be added here too.
    """
    expected_columns = {
        "id",
        "vps_address",
        "vps_instance_id",
        "agent_id",
        "host_id",
        "host_name",
        "ssh_port",
        "ssh_user",
        "container_ssh_port",
        "status",
        "attributes",
        "leased_to_user",
        "leased_at",
        "released_at",
        "created_at",
        "region",
        "backend_kind",
        "bare_metal_server_id",
        "lima_instance_name",
        "lima_disk_name",
    }
    assert set(_POOL_HOST_LIST_COLUMNS) == expected_columns, (
        "`pool list` columns drifted from the pool_hosts schema; add (or remove) the column in "
        f"_POOL_HOST_LIST_COLUMNS so the SELECT and JSON keys stay complete. "
        f"missing={expected_columns - set(_POOL_HOST_LIST_COLUMNS)} "
        f"unexpected={set(_POOL_HOST_LIST_COLUMNS) - expected_columns}"
    )
    assert len(_POOL_HOST_LIST_COLUMNS) == len(set(_POOL_HOST_LIST_COLUMNS)), (
        f"_POOL_HOST_LIST_COLUMNS has duplicate entries: {_POOL_HOST_LIST_COLUMNS}"
    )


def _insert_column_to_value(values: tuple[object, ...]) -> dict[str, object]:
    """Pair _INSERT_POOL_HOST_SQL's %s columns with a built values tuple.

    Parses the column list and the VALUES clause out of the SQL, keeps only
    the placeholder (``%s`` / ``%s::jsonb``) columns in order, and zips them
    with ``values`` -- so a test can assert "this column got that value"
    robustly even if the column order changes.
    """
    columns_part = _INSERT_POOL_HOST_SQL.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_part.split(",")]
    values_part = _INSERT_POOL_HOST_SQL.split("VALUES (", 1)[1].rsplit(")", 1)[0]
    value_tokens = [t.strip() for t in values_part.split(",")]
    placeholder_columns = [col for col, tok in zip(columns, value_tokens, strict=False) if "%s" in tok]
    assert len(placeholder_columns) == len(values), (
        f"placeholder columns {placeholder_columns} do not line up with values {values}"
    )
    return dict(zip(placeholder_columns, values, strict=False))


def test_pool_host_insert_writes_service_name_into_vps_instance_id() -> None:
    """vps_instance_id must be the OVH service name (vps_address), never host_id.

    Regression test for the bug where the bake wrote the mngr ``host_id``
    (``host-...``) into ``vps_instance_id``. The connector's OVH teardown
    (``vps_urn_for`` / ``set_delete_at_expiration``) keys on this column, so a
    ``host-...`` value made every cancel silently 404 -- VPSes were never
    cancelled and kept billing. Uses the real ``host-``/``vps-`` shapes so a
    future re-swap of the two identical-looking arguments is caught.
    """
    values = build_pool_host_insert_values(
        row_id="11111111-1111-1111-1111-111111111111",
        vps_address="vps-deadbeef.vps.ovh.us",
        agent_id="agent-aaaa",
        host_id="host-bbbb",
        host_name="my-host",
        container_ssh_port=_CONTAINER_SSH_PORT,
        attributes_json="{}",
        region="US-EAST-VA",
        outer_host_public_key="ssh-ed25519 AAAAouter",
        container_host_public_key="ssh-ed25519 AAAAcontainer",
    )
    column_to_value = _insert_column_to_value(values)
    assert column_to_value["vps_instance_id"] == "vps-deadbeef.vps.ovh.us"
    assert column_to_value["region"] == "US-EAST-VA"
    assert column_to_value["vps_instance_id"] != "host-bbbb"
    # Sanity: host_id still lands in its own column.
    assert column_to_value["host_id"] == "host-bbbb"
    assert column_to_value["vps_address"] == "vps-deadbeef.vps.ovh.us"
    # The baked host keys land in their own columns for strict pinning at lease time.
    assert column_to_value["outer_host_public_key"] == "ssh-ed25519 AAAAouter"
    assert column_to_value["container_host_public_key"] == "ssh-ed25519 AAAAcontainer"


def test_pool_create_backend_defaults_to_slice() -> None:
    """The default --backend is ``slice`` -- OVH VPS baking is deprecated."""
    backend_option = next(param for param in pool.commands["create"].params if param.name == "backend")
    assert backend_option.default == "slice"


def test_pool_create_rejects_ovh_vps_backend(tmp_path: Any) -> None:
    """Explicitly requesting --backend ovh_vps fails with a deprecation message pointing at slice."""
    key_file = tmp_path / "mgmt.pub"
    key_file.write_text("ssh-ed25519 AAAA... operator@host\n")
    result = CliRunner().invoke(
        pool,
        [
            "create",
            "--backend",
            "ovh_vps",
            "--count",
            "1",
            "--region",
            "US-EAST-VA",
            "--management-public-key-file",
            str(key_file),
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "deprecated" in result.output
    assert "--backend slice" in result.output


def _slice_create_args(extra: list[str]) -> list[str]:
    """Base ``pool create --backend slice`` argv (with a DSN + server-id so resolution succeeds).

    Carries no identity attributes: repo_url / repo_branch_or_tag are derived from
    the bake source (--from-tag / --workspace-dir), never passed in --attributes.
    """
    return [
        "create",
        "--backend",
        "slice",
        "--count",
        "1",
        "--region",
        "US-EAST-VA",
        "--server-id",
        "11111111-1111-1111-1111-111111111111",
        "--database-url",
        "postgres://example",
        *extra,
    ]


def test_pool_create_slice_backend_requires_server_id() -> None:
    """``--server-id`` is required for the slice backend (we never auto-select a box)."""
    args = [
        "create",
        "--backend",
        "slice",
        "--count",
        "1",
        "--region",
        "US-EAST-VA",
        "--database-url",
        "postgres://example",
        "--from-tag",
        "v0.3.0",
    ]
    result = CliRunner().invoke(pool, args)
    assert result.exit_code != 0
    assert "--server-id is required for --backend slice" in result.output


def test_pool_create_requires_a_bake_source_selector() -> None:
    """Neither --from-tag nor --workspace-dir is a usage error (exactly one is required)."""
    result = CliRunner().invoke(pool, _slice_create_args([]))
    assert result.exit_code != 0
    assert "--from-tag" in result.output and "--workspace-dir" in result.output


def test_pool_create_rejects_both_bake_source_selectors(tmp_path: Any) -> None:
    """Passing both --from-tag and --workspace-dir is a usage error."""
    result = CliRunner().invoke(pool, _slice_create_args(["--from-tag", "v0.3.0", "--workspace-dir", str(tmp_path)]))
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_pool_create_slice_backend_rejects_tag() -> None:
    """``--tag`` is OVH-only; using it with ``--backend slice`` is a usage error before any work."""
    result = CliRunner().invoke(pool, _slice_create_args(["--tag", "k=v"]))
    assert result.exit_code != 0
    assert "--tag is not applicable to --backend slice" in result.output


def test_pool_create_slice_backend_rejects_management_key(tmp_path: Any) -> None:
    """Slices authorize the pool key from POOL_SSH_PRIVATE_KEY, so the mgmt-key flag is rejected."""
    key_file = tmp_path / "mgmt.pub"
    key_file.write_text("ssh-ed25519 AAAA... operator@host\n")
    result = CliRunner().invoke(pool, _slice_create_args(["--management-public-key-file", str(key_file)]))
    assert result.exit_code != 0
    assert "--management-public-key-file is not applicable to --backend slice" in result.output


def test_resolve_underlying_teardown_slice_destroys_vm() -> None:
    """A slice row tears down its lima VM (not an OVH cancel) -- the regression this fix closes."""
    assert (
        resolve_underlying_teardown(backend_kind="slice", is_skip_requested=False)
        == PoolHostUnderlyingTeardown.SLICE_VM
    )


def test_resolve_underlying_teardown_ovh_vps_cancels_vps() -> None:
    assert (
        resolve_underlying_teardown(backend_kind="ovh_vps", is_skip_requested=False)
        == PoolHostUnderlyingTeardown.OVH_VPS
    )


def test_resolve_underlying_teardown_legacy_null_backend_treated_as_ovh() -> None:
    """Rows written before the backend_kind column existed (None) take the OVH path."""
    assert (
        resolve_underlying_teardown(backend_kind=None, is_skip_requested=False) == PoolHostUnderlyingTeardown.OVH_VPS
    )


def test_resolve_underlying_teardown_skip_overrides_every_backend() -> None:
    """--skip-vps-cancel drops the row only, regardless of backend."""
    for backend_kind in ("slice", "ovh_vps", None):
        assert (
            resolve_underlying_teardown(backend_kind=backend_kind, is_skip_requested=True)
            == PoolHostUnderlyingTeardown.NONE
        )
