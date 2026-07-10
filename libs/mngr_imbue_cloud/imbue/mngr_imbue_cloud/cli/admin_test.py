"""Tests for ``mngr imbue_cloud admin pool ...`` provider-generic helpers.

We don't exercise the full subprocess pipeline (that needs a real bare-metal box
+ a real Neon DB); instead we cover the small pure helpers that encode the
contract this command commits to: region validation, the list-column coverage,
and the CLI signature.
"""

from typing import Any

from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.admin import _POOL_HOST_LIST_COLUMNS
from imbue.mngr_imbue_cloud.cli.admin import pool


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
            "--server-id",
            "11111111-1111-1111-1111-111111111111",
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


def test_pool_list_covers_every_pool_host_column() -> None:
    """`pool list` must surface every pool_hosts column, not a hand-maintained subset.

    Regression test for the drift where region and the slice identifiers
    (bare_metal_server_id / lima_instance_name / lima_disk_name) were absent from the
    list output. Because `_POOL_HOST_LIST_COLUMNS` now drives both the SELECT and the
    emitted JSON keys, asserting it equals the full schema keeps the two in lockstep
    and forces any new pool_hosts migration column to be added here too.
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


def _slice_create_args(extra: list[str]) -> list[str]:
    """Base ``pool create`` argv (with a DSN + server-id so resolution succeeds).

    Carries no identity attributes: repo_url / repo_branch_or_tag are derived from
    the bake source (--from-tag / --workspace-dir), never passed in --attributes.
    """
    return [
        "create",
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


def test_pool_create_requires_server_id() -> None:
    """``--server-id`` is required (we never auto-select a box)."""
    args = [
        "create",
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
    assert "--server-id is required" in result.output


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


def test_pool_destroy_requires_at_least_one_id() -> None:
    result = CliRunner().invoke(pool, ["destroy", "--database-url", "postgres://example"])
    assert result.exit_code != 0
    assert "POOL_HOST_IDS" in result.output


def test_pool_destroy_rejects_the_removed_skip_vps_cancel_flag() -> None:
    """The vestigial --skip-vps-cancel flag was replaced by --drop-row-only (clean break)."""
    result = CliRunner().invoke(pool, ["destroy", "row-1", "--skip-vps-cancel"])
    assert result.exit_code != 0
    assert "No such option" in result.output


def test_pool_destroy_rejects_nonpositive_max_concurrency() -> None:
    result = CliRunner().invoke(
        pool,
        ["destroy", "row-1", "--database-url", "postgres://example", "--max-concurrency", "0"],
    )
    assert result.exit_code != 0
    assert "--max-concurrency must be positive" in result.output
