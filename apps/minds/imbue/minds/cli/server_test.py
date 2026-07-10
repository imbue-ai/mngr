"""Tests for the ``minds server`` env-aware wrapper.

Mirrors the ``minds pool`` wrapper tests: the argv-construction logic lives in
pure ``build_*_args`` helpers so the forwarding contract is verified directly;
the click commands are exercised only for their activation guard.
"""

from pathlib import Path

from click.testing import CliRunner

from imbue.minds.cli.server import build_server_list_admin_args
from imbue.minds.cli.server import build_server_prep_admin_args
from imbue.minds.cli.server import server


def test_build_server_list_admin_args_forwards_dsn_when_present() -> None:
    assert build_server_list_admin_args(database_url=None) == ["list"]
    assert build_server_list_admin_args(database_url="postgres://x") == [
        "list",
        "--database-url",
        "postgres://x",
    ]


def test_build_server_prep_admin_args_minimal() -> None:
    # Only --server-id is required; omitted overrides are NOT forwarded, so the
    # admin CLI's own defaults (pinned lima release / guest image) stay in charge.
    assert build_server_prep_admin_args(
        server_id="feb11eae-a20a-4d9e-a0a3-ce06a526956c",
        database_url=None,
        lima_version=None,
        slice_base_image_url=None,
    ) == ["prep", "--server-id", "feb11eae-a20a-4d9e-a0a3-ce06a526956c"]


def test_build_server_prep_admin_args_forwards_overrides() -> None:
    args = build_server_prep_admin_args(
        server_id="feb11eae-a20a-4d9e-a0a3-ce06a526956c",
        database_url="postgres://x",
        lima_version="1.0.7",
        slice_base_image_url="https://example.com/img.qcow2",
    )
    assert args[args.index("--database-url") + 1] == "postgres://x"
    assert args[args.index("--lima-version") + 1] == "1.0.7"
    assert args[args.index("--slice-base-image-url") + 1] == "https://example.com/img.qcow2"


def test_server_list_requires_activated_env(_isolated_env: Path) -> None:
    result = CliRunner().invoke(server, ["list", "--database-url", "postgres://example"])
    assert result.exit_code != 0
    assert "No minds env is activated" in result.output


def test_server_prep_requires_activated_env(_isolated_env: Path) -> None:
    result = CliRunner().invoke(
        server,
        ["prep", "--server-id", "feb11eae-a20a-4d9e-a0a3-ce06a526956c", "--database-url", "postgres://example"],
    )
    assert result.exit_code != 0
    assert "No minds env is activated" in result.output


def test_server_prep_requires_server_id(_isolated_env: Path) -> None:
    result = CliRunner().invoke(server, ["prep"])
    assert result.exit_code != 0
    assert "--server-id" in result.output
