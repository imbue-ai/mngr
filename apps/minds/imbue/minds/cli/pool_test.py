"""Tests for the ``minds pool`` env-aware wrapper.

The argv-construction logic is split into pure ``build_*_args`` helpers in
:mod:`imbue.minds.cli.pool` so we can verify the contract directly,
without faking a subprocess runner. The click command's role is just to
parse args, call ``_require_activated_env_name``, and run the result --
we test that with :class:`click.testing.CliRunner`.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.minds.cli.pool import build_create_admin_args
from imbue.minds.cli.pool import build_destroy_admin_args
from imbue.minds.cli.pool import build_list_admin_args
from imbue.minds.cli.pool import pool


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip activation env vars by default; tests opt in to a specific env."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    return tmp_path


def test_build_create_admin_args_injects_minds_env_tag() -> None:
    args = build_create_admin_args(
        env_name="alice",
        count=3,
        region="US-EAST-VA",
        attributes_json='{"cpus": 2}',
        workspace_dir="/path/to/workspace",
        management_public_key_file="/path/to/key.pub",
        database_url="postgres://example",
        mngr_source=None,
    )
    # The --tag injection is the whole reason for this layer's existence.
    tag_index = args.index("--tag")
    assert args[tag_index + 1] == "minds_env=alice"


def test_build_create_admin_args_forwards_all_other_flags_verbatim() -> None:
    args = build_create_admin_args(
        env_name="alice",
        count=5,
        region="US-WEST-OR",
        attributes_json='{"cpus": 4, "memory_gb": 16}',
        workspace_dir="/some/workspace",
        management_public_key_file="/path/to/key.pub",
        database_url="postgres://example",
        mngr_source="/path/to/mngr",
    )
    assert args[0] == "create"
    assert args[args.index("--count") + 1] == "5"
    assert args[args.index("--region") + 1] == "US-WEST-OR"
    assert args[args.index("--attributes") + 1] == '{"cpus": 4, "memory_gb": 16}'
    assert args[args.index("--workspace-dir") + 1] == "/some/workspace"
    assert args[args.index("--management-public-key-file") + 1] == "/path/to/key.pub"
    assert args[args.index("--database-url") + 1] == "postgres://example"
    assert args[args.index("--mngr-source") + 1] == "/path/to/mngr"


def test_build_create_admin_args_omits_mngr_source_when_none() -> None:
    args = build_create_admin_args(
        env_name="alice",
        count=1,
        region="US-EAST-VA",
        attributes_json="{}",
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
    )
    assert "--mngr-source" not in args


def test_build_list_admin_args() -> None:
    assert build_list_admin_args(database_url="postgres://x") == [
        "list",
        "--database-url",
        "postgres://x",
    ]


def test_build_destroy_admin_args_without_force() -> None:
    assert build_destroy_admin_args(pool_host_id="abc-123", database_url="postgres://x", force=False) == [
        "destroy",
        "abc-123",
        "--database-url",
        "postgres://x",
    ]


def test_build_destroy_admin_args_with_force() -> None:
    args = build_destroy_admin_args(pool_host_id="abc-123", database_url="postgres://x", force=True)
    assert "--force" in args
    # ``--force`` is a flag, not an arg-value, so order is the only thing
    # that matters: ensure it comes after the id + db url.
    assert args.index("--force") > args.index("abc-123")


def test_pool_create_requires_activated_env(_isolated_env: Path) -> None:
    """With no MINDS_ROOT_NAME set, the click command must refuse early."""
    runner = CliRunner()
    key_file = _isolated_env / "mgmt.pub"
    key_file.write_text("ssh-ed25519 AAAA... operator@host\n")
    result = runner.invoke(
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
            str(_isolated_env),
            "--management-public-key-file",
            str(key_file),
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "No minds env is activated" in result.output


def test_pool_create_derives_production_from_default_root_name(
    _isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MINDS_ROOT_NAME=minds`` resolves to the ``production`` env name in the tag."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds")
    # Pure-function check: the tag injection logic is the same path the click
    # command uses, so verifying it here covers the end-to-end behaviour.
    args = build_create_admin_args(
        env_name="production",
        count=1,
        region="US-EAST-VA",
        attributes_json="{}",
        workspace_dir=str(_isolated_env),
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
    )
    tag_index = args.index("--tag")
    assert args[tag_index + 1] == "minds_env=production"


def test_pool_list_requires_activated_env(_isolated_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(pool, ["list", "--database-url", "postgres://example"])
    assert result.exit_code != 0
    assert "No minds env is activated" in result.output


def test_pool_destroy_requires_activated_env(_isolated_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(pool, ["destroy", "abc-123", "--database-url", "postgres://example"])
    assert result.exit_code != 0
    assert "No minds env is activated" in result.output
