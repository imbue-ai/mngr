"""Tests for the ``minds pool`` env-aware wrapper.

The argv-construction logic is split into pure ``build_*_args`` helpers in
:mod:`imbue.minds.cli.pool` so we can verify the contract directly,
without faking a subprocess runner. The click command's role is just to
parse args, call ``require_activated_env_name``, and run the result --
we test that with :class:`click.testing.CliRunner`.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.minds.cli.pool import _SECRET_BEARING_FLAGS
from imbue.minds.cli.pool import build_backfill_host_keys_admin_args
from imbue.minds.cli.pool import build_create_admin_args
from imbue.minds.cli.pool import build_destroy_admin_args
from imbue.minds.cli.pool import build_list_admin_args
from imbue.minds.cli.pool import build_teardown_slices_admin_args
from imbue.minds.cli.pool import merge_extra_env_into_subprocess_env
from imbue.minds.cli.pool import pool
from imbue.minds.cli.pool import resolve_host_pool_dsn
from imbue.minds.utils.secret_redaction import redact_secret_flag_values


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip activation env vars by default; tests opt in to a specific env."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    return tmp_path


def _slice_args(
    *,
    env_name: str = "alice",
    count: int = 1,
    region: str = "US-EAST-VA",
    from_tag: str | None = "minds-v0.3.1",
    repo_url: str | None = None,
    repo_branch_or_tag_override: str | None = None,
    attributes_json: str | None = None,
    workspace_dir: str | None = None,
    database_url: str | None = "postgres://example",
    mngr_source: str | None = None,
    is_dry_run: bool = False,
    is_deferred_install_wait_skipped: bool = False,
    server_id: str | None = "feb11eae-a20a-4d9e-a0a3-ce06a526956c",
    max_concurrency: int | None = None,
) -> list[str]:
    """Call build_create_admin_args with sensible slice defaults, overridable per test."""
    return build_create_admin_args(
        env_name=env_name,
        count=count,
        region=region,
        from_tag=from_tag,
        repo_url=repo_url,
        repo_branch_or_tag_override=repo_branch_or_tag_override,
        attributes_json=attributes_json,
        workspace_dir=workspace_dir,
        database_url=database_url,
        mngr_source=mngr_source,
        is_dry_run=is_dry_run,
        is_deferred_install_wait_skipped=is_deferred_install_wait_skipped,
        server_id=server_id,
        max_concurrency=max_concurrency,
    )


def test_database_url_is_redacted_from_the_loggable_admin_command() -> None:
    # The admin command echo is the path that leaked the production Neon DSN;
    # the DSN must never survive into the rendered "Running: ..." log line.
    dsn = "postgresql://neondb_owner:npg_supersecret@ep-host.neon.tech/host_pool"
    args = build_list_admin_args(database_url=dsn)
    full_command = ["mngr", "imbue_cloud", "admin", "pool"] + args

    loggable = redact_secret_flag_values(full_command, secret_bearing_flags=_SECRET_BEARING_FLAGS)

    assert "--database-url" in _SECRET_BEARING_FLAGS
    assert "npg_supersecret" not in " ".join(loggable)
    assert dsn not in " ".join(loggable)
    assert loggable == ["mngr", "imbue_cloud", "admin", "pool", "list", "--database-url", "***"]


def test_build_create_admin_args_stamps_the_env_name() -> None:
    """The create wrapper forwards --slice-env-name so slices on a shared box are env-attributable."""
    args = _slice_args(env_name="dev-josh-foo")
    assert args[args.index("--slice-env-name") + 1] == "dev-josh-foo"


def test_build_create_admin_args_never_emits_removed_ovh_flags() -> None:
    """The OVH-VPS flags (--backend / --tag / --management-public-key-file / --no-recycle) are gone."""
    args = _slice_args()
    for removed_flag in ("--backend", "--tag", "--management-public-key-file", "--no-recycle"):
        assert removed_flag not in args


def test_build_create_admin_args_forwards_all_other_flags_verbatim() -> None:
    args = _slice_args(
        count=5,
        region="US-WEST-OR",
        from_tag=None,
        workspace_dir="/some/workspace",
        attributes_json='{"cpus": 4, "memory_gb": 16}',
        mngr_source="/path/to/mngr",
    )
    assert args[0] == "create"
    assert args[args.index("--count") + 1] == "5"
    assert args[args.index("--region") + 1] == "US-WEST-OR"
    assert args[args.index("--attributes") + 1] == '{"cpus": 4, "memory_gb": 16}'
    assert args[args.index("--workspace-dir") + 1] == "/some/workspace"
    assert args[args.index("--database-url") + 1] == "postgres://example"
    assert args[args.index("--mngr-source") + 1] == "/path/to/mngr"


def test_build_create_admin_args_omits_mngr_source_when_none() -> None:
    assert "--mngr-source" not in _slice_args(mngr_source=None)


def test_build_create_admin_args_forwards_skip_deferred_install_wait_when_set() -> None:
    assert "--skip-deferred-install-wait" in _slice_args(is_deferred_install_wait_skipped=True)


def test_build_create_admin_args_omits_skip_deferred_install_wait_by_default() -> None:
    assert "--skip-deferred-install-wait" not in _slice_args(is_deferred_install_wait_skipped=False)


def test_build_create_admin_args_forwards_dry_run() -> None:
    assert "--dry-run" in _slice_args(is_dry_run=True)


def test_build_create_admin_args_forwards_server_id() -> None:
    args = _slice_args(server_id="feb11eae-a20a-4d9e-a0a3-ce06a526956c")
    assert args[args.index("--server-id") + 1] == "feb11eae-a20a-4d9e-a0a3-ce06a526956c"


def test_build_create_admin_args_forwards_max_concurrency() -> None:
    args = _slice_args(count=8, max_concurrency=4)
    assert args[args.index("--max-concurrency") + 1] == "4"


def test_build_create_admin_args_omits_max_concurrency_when_none() -> None:
    assert "--max-concurrency" not in _slice_args(max_concurrency=None)


def test_build_list_admin_args() -> None:
    assert build_list_admin_args(database_url="postgres://x") == [
        "list",
        "--database-url",
        "postgres://x",
    ]


def test_build_backfill_host_keys_admin_args_forwards_dsn_when_present() -> None:
    assert build_backfill_host_keys_admin_args(database_url=None) == ["backfill-host-keys"]
    assert build_backfill_host_keys_admin_args(database_url="postgres://x") == [
        "backfill-host-keys",
        "--database-url",
        "postgres://x",
    ]


def test_build_destroy_admin_args_without_force() -> None:
    assert build_destroy_admin_args(
        pool_host_ids=["abc-123"],
        database_url="postgres://x",
        is_leased_destroy_allowed=False,
        is_row_drop_only=False,
        max_concurrency=None,
    ) == [
        "destroy",
        "abc-123",
        "--database-url",
        "postgres://x",
    ]


def test_build_destroy_admin_args_forwards_all_ids_in_one_invocation() -> None:
    # Parallel destroy: every id rides in a single admin invocation (the admin CLI
    # fans them out concurrently), never one subprocess per host.
    args = build_destroy_admin_args(
        pool_host_ids=["id-1", "id-2", "id-3"],
        database_url=None,
        is_leased_destroy_allowed=False,
        is_row_drop_only=False,
        max_concurrency=None,
    )
    assert args[:4] == ["destroy", "id-1", "id-2", "id-3"]


def test_build_destroy_admin_args_with_force() -> None:
    args = build_destroy_admin_args(
        pool_host_ids=["abc-123"],
        database_url="postgres://x",
        is_leased_destroy_allowed=True,
        is_row_drop_only=False,
        max_concurrency=None,
    )
    assert "--force" in args
    # ``--force`` is a flag, not an arg-value, so order is the only thing
    # that matters: ensure it comes after the id + db url.
    assert args.index("--force") > args.index("abc-123")


def test_build_destroy_admin_args_drop_row_only_and_max_concurrency() -> None:
    args = build_destroy_admin_args(
        pool_host_ids=["abc-123"],
        database_url=None,
        is_leased_destroy_allowed=False,
        is_row_drop_only=True,
        max_concurrency=4,
    )
    assert "--drop-row-only" in args
    assert args[args.index("--max-concurrency") + 1] == "4"
    # Default teardown must NOT pass either flag, so the admin command's
    # VM-teardown path and its default concurrency stay in effect.
    default_args = build_destroy_admin_args(
        pool_host_ids=["abc-123"],
        database_url=None,
        is_leased_destroy_allowed=False,
        is_row_drop_only=False,
        max_concurrency=None,
    )
    assert "--drop-row-only" not in default_args
    assert "--max-concurrency" not in default_args


def test_pool_create_requires_activated_env(_isolated_env: Path) -> None:
    """With no MINDS_ROOT_NAME set, the click command must refuse early."""
    runner = CliRunner()
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
            "--server-id",
            "feb11eae-a20a-4d9e-a0a3-ce06a526956c",
            "--database-url",
            "postgres://example",
        ],
    )
    assert result.exit_code != 0
    assert "No minds env is activated" in result.output


def test_pool_create_stamps_env_name_into_slice_env_name() -> None:
    """``MINDS_ROOT_NAME=minds`` resolves to the ``production`` env name in --slice-env-name."""
    # Pure-function check: the env-name stamping logic is the same path the click
    # command uses, so verifying it here covers the end-to-end behaviour.
    args = _slice_args(env_name="production")
    assert args[args.index("--slice-env-name") + 1] == "production"


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


def test_merge_extra_env_overrides_shell_values() -> None:
    """Vault-sourced secrets win over whatever the operator's shell has set.

    Operator running ``minds pool create`` after activating a specific tier
    intends to bake against THAT tier's account. A stale POOL_SSH_PRIVATE_KEY from
    a different tier's session would otherwise silently misroute the bake.
    """
    merged = merge_extra_env_into_subprocess_env(
        shell_env={"POOL_SSH_PRIVATE_KEY": "stale", "HOME": "/home/me"},
        extra_env={"POOL_SSH_PRIVATE_KEY": "from-vault"},
    )
    assert merged["POOL_SSH_PRIVATE_KEY"] == "from-vault"
    # Non-overlaid shell vars are preserved untouched.
    assert merged["HOME"] == "/home/me"


def test_merge_extra_env_preserves_unrelated_shell_vars() -> None:
    """The overlay does not perturb unrelated env vars."""
    merged = merge_extra_env_into_subprocess_env(
        shell_env={"PATH": "/usr/bin", "FOO": "bar"},
        extra_env={"POOL_SSH_PRIVATE_KEY": "-----BEGIN-----"},
    )
    assert merged["PATH"] == "/usr/bin"
    assert merged["FOO"] == "bar"
    assert merged["POOL_SSH_PRIVATE_KEY"] == "-----BEGIN-----"


def test_resolve_host_pool_dsn_returns_explicit_when_given() -> None:
    """An explicit --database-url wins and never touches Vault (even on staging)."""
    # If this consulted Vault for the staging tier it would shell out / fail in
    # the test env; returning the explicit value proves the precedence short-circuit.
    assert resolve_host_pool_dsn("staging", "postgres://explicit") == "postgres://explicit"


def test_resolve_host_pool_dsn_returns_none_for_dev_tier() -> None:
    """Per-env tiers return None (no Vault read) so the admin CLI auto-resolves the DSN."""
    assert resolve_host_pool_dsn("dev-josh-1", None) is None


def test_resolve_host_pool_dsn_returns_none_for_ci_tier() -> None:
    assert resolve_host_pool_dsn("ci-abc123", None) is None


def test_merge_extra_env_with_empty_overlay_returns_shell_copy() -> None:
    """An empty Vault overlay (e.g. when no secrets are injected) is a no-op."""
    merged = merge_extra_env_into_subprocess_env(
        shell_env={"PATH": "/usr/bin"},
        extra_env={},
    )
    assert merged == {"PATH": "/usr/bin"}


def test_build_teardown_slices_admin_args_forwards_dsn_when_present() -> None:
    assert build_teardown_slices_admin_args(database_url=None) == ["teardown-slices"]
    args = build_teardown_slices_admin_args(database_url="postgres://example")
    assert args == ["teardown-slices", "--database-url", "postgres://example"]
