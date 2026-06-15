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

from imbue.mngr_imbue_cloud.cli.admin import _CONTAINER_SSH_PORT
from imbue.mngr_imbue_cloud.cli.admin import _INSERT_POOL_HOST_SQL
from imbue.mngr_imbue_cloud.cli.admin import _ufw_provision_commands
from imbue.mngr_imbue_cloud.cli.admin import build_extra_tags_env_value
from imbue.mngr_imbue_cloud.cli.admin import build_pool_host_insert_values
from imbue.mngr_imbue_cloud.cli.admin import pool


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
    )
    column_to_value = _insert_column_to_value(values)
    assert column_to_value["vps_instance_id"] == "vps-deadbeef.vps.ovh.us"
    assert column_to_value["region"] == "US-EAST-VA"
    assert column_to_value["vps_instance_id"] != "host-bbbb"
    # Sanity: host_id still lands in its own column.
    assert column_to_value["host_id"] == "host-bbbb"
    assert column_to_value["vps_address"] == "vps-deadbeef.vps.ovh.us"


def test_pool_create_rejects_malformed_tag(tmp_path: Any) -> None:
    """A ``--tag`` value without ``=`` aborts the bake before any subprocess work."""
    runner = CliRunner()
    key_file = tmp_path / "mgmt.pub"
    key_file.write_text("ssh-ed25519 AAAA... operator@host\n")
    result = runner.invoke(
        pool,
        [
            "create",
            "--count",
            "1",
            "--region",
            "US-EAST-VA",
            "--tag",
            "no-equals",
            "--attributes",
            "{}",
            "--workspace-dir",
            str(tmp_path),
            "--management-public-key-file",
            str(key_file),
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output or "KEY=VALUE" in str(result.exception)


def _slice_create_args(extra: list[str]) -> list[str]:
    """Base ``pool create --backend slice`` argv (with a DSN so resolution succeeds)."""
    return [
        "create",
        "--backend",
        "slice",
        "--count",
        "1",
        "--region",
        "US-EAST-VA",
        "--attributes",
        '{"repo_branch_or_tag":"main"}',
        "--database-url",
        "postgres://example",
        *extra,
    ]


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


def test_pool_create_ovh_backend_requires_management_key() -> None:
    """The OVH backend still requires the management public key (now validated, not click-required)."""
    result = CliRunner().invoke(
        pool,
        [
            "create",
            "--count",
            "1",
            "--region",
            "US-EAST-VA",
            "--attributes",
            "{}",
            "--workspace-dir",
            ".",
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "--management-public-key-file is required for --backend ovh_vps" in result.output
