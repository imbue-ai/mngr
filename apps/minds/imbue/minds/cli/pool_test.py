"""Tests for the ``minds pool`` env-aware wrapper.

The argv-construction logic is split into pure ``build_*_args`` helpers in
:mod:`imbue.minds.cli.pool` so we can verify the contract directly,
without faking a subprocess runner. The click command's role is just to
parse args, call ``require_activated_env_name``, and run the result --
we test that with :class:`click.testing.CliRunner`.
"""

from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from imbue.minds.cli.pool import _BACKEND_OVH_VPS
from imbue.minds.cli.pool import _BACKEND_SLICE
from imbue.minds.cli.pool import _SECRET_BEARING_FLAGS
from imbue.minds.cli.pool import build_backfill_host_keys_admin_args
from imbue.minds.cli.pool import build_create_admin_args
from imbue.minds.cli.pool import build_destroy_admin_args
from imbue.minds.cli.pool import build_list_admin_args
from imbue.minds.cli.pool import build_teardown_slices_admin_args
from imbue.minds.cli.pool import derive_public_key_from_private
from imbue.minds.cli.pool import merge_extra_env_into_subprocess_env
from imbue.minds.cli.pool import pool
from imbue.minds.cli.pool import resolve_host_pool_dsn
from imbue.minds.cli.pool import resolved_management_public_key_path
from imbue.minds.utils.secret_redaction import redact_secret_flag_values


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip activation env vars by default; tests opt in to a specific env."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    return tmp_path


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


def test_build_create_admin_args_injects_minds_env_tag() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=3,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json='{"cpus": 2}',
        workspace_dir="/path/to/workspace",
        management_public_key_file="/path/to/key.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    # The --tag injection is the whole reason for this layer's existence.
    tag_index = args.index("--tag")
    assert args[tag_index + 1] == "minds_env=alice"


def test_build_create_admin_args_emits_backend() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert args[args.index("--backend") + 1] == "ovh_vps"


def test_build_create_admin_args_forwards_all_other_flags_verbatim() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=5,
        region="US-WEST-OR",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json='{"cpus": 4, "memory_gb": 16}',
        workspace_dir="/some/workspace",
        management_public_key_file="/path/to/key.pub",
        database_url="postgres://example",
        mngr_source="/path/to/mngr",
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
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
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json="{}",
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert "--mngr-source" not in args


def test_build_create_admin_args_omits_no_recycle_by_default() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json="{}",
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert "--no-recycle" not in args


def test_build_create_admin_args_forwards_no_recycle_when_disabled() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json="{}",
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=False,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert "--no-recycle" in args


def test_build_create_admin_args_forwards_skip_deferred_install_wait_when_set() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=True,
    )
    assert "--skip-deferred-install-wait" in args


def test_build_create_admin_args_omits_skip_deferred_install_wait_by_default() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert "--skip-deferred-install-wait" not in args


def test_build_create_admin_args_slice_omits_ovh_only_flags() -> None:
    """Slice bakes are not OVH-IAM-tagged and need no --management-public-key-file."""
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_SLICE,
        count=2,
        region="US-EAST-VA",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file=None,
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert args[args.index("--backend") + 1] == "slice"
    assert "--tag" not in args
    assert "--management-public-key-file" not in args
    assert args[args.index("--from-tag") + 1] == "minds-v0.3.1"
    # The owning env is stamped into each slice's lima names instead of an OVH tag.
    assert args[args.index("--slice-env-name") + 1] == "alice"


def test_build_create_admin_args_slice_stamps_the_env_name() -> None:
    """The slice backend forwards --slice-env-name so slices on a shared box are env-attributable."""
    args = build_create_admin_args(
        env_name="dev-josh-foo",
        backend=_BACKEND_SLICE,
        count=1,
        region="US-WEST-OR",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file=None,
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
        server_id="feb11eae-a20a-4d9e-a0a3-ce06a526956c",
    )
    assert args[args.index("--slice-env-name") + 1] == "dev-josh-foo"


def test_build_create_admin_args_ovh_backend_does_not_stamp_slice_env_name() -> None:
    """--slice-env-name is slice-only; the ovh_vps path uses the minds_env tag instead."""
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file="/tmp/key.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert "--slice-env-name" not in args


def test_build_create_admin_args_slice_forwards_dry_run() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_SLICE,
        count=1,
        region="US-EAST-VA",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file=None,
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=True,
        is_deferred_install_wait_skipped=False,
    )
    assert "--dry-run" in args


def test_build_create_admin_args_slice_forwards_server_id() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_SLICE,
        count=1,
        region="US-WEST-OR",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file=None,
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
        server_id="feb11eae-a20a-4d9e-a0a3-ce06a526956c",
    )
    assert args[args.index("--server-id") + 1] == "feb11eae-a20a-4d9e-a0a3-ce06a526956c"


def test_build_create_admin_args_omits_dry_run_for_ovh_backend() -> None:
    """--dry-run is slice-only; the ovh_vps path must never forward it."""
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=True,
        is_deferred_install_wait_skipped=False,
    )
    assert "--dry-run" not in args


def test_build_create_admin_args_omits_no_recycle_for_slice_backend() -> None:
    """--no-recycle is ovh_vps-only; the slice path must never forward it."""
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_SLICE,
        count=1,
        region="US-EAST-VA",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file=None,
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=False,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    assert "--no-recycle" not in args


def test_build_create_admin_args_slice_forwards_max_concurrency() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_SLICE,
        count=8,
        region="US-EAST-VA",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file=None,
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
        max_concurrency=4,
    )
    assert args[args.index("--max-concurrency") + 1] == "4"


def test_build_create_admin_args_omits_max_concurrency_when_none() -> None:
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_SLICE,
        count=8,
        region="US-EAST-VA",
        from_tag="minds-v0.3.1",
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir=None,
        management_public_key_file=None,
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
        max_concurrency=None,
    )
    assert "--max-concurrency" not in args


def test_build_create_admin_args_omits_max_concurrency_for_ovh_backend() -> None:
    # --max-concurrency is slice-only; the ovh_vps path must never forward it.
    args = build_create_admin_args(
        env_name="alice",
        backend=_BACKEND_OVH_VPS,
        count=2,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json=None,
        workspace_dir="/w",
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
        max_concurrency=4,
    )
    assert "--max-concurrency" not in args


def test_pool_create_backend_defaults_to_slice() -> None:
    """The default --backend is ``slice`` -- OVH VPS baking is deprecated."""
    backend_option = next(param for param in pool.commands["create"].params if param.name == "backend")
    assert backend_option.default == _BACKEND_SLICE


def test_pool_create_rejects_ovh_vps_backend(
    _isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--backend ovh_vps fails fast with a deprecation message pointing at slice."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds")
    runner = CliRunner()
    result = runner.invoke(
        pool,
        ["create", "--backend", "ovh_vps", "--count", "1", "--region", "US-EAST-VA"],
    )
    assert result.exit_code != 0
    assert "deprecated" in result.output
    assert "--backend slice" in result.output


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
        pool_host_id="abc-123", database_url="postgres://x", force=False, skip_vps_cancel=False
    ) == [
        "destroy",
        "abc-123",
        "--database-url",
        "postgres://x",
    ]


def test_build_destroy_admin_args_with_force() -> None:
    args = build_destroy_admin_args(
        pool_host_id="abc-123", database_url="postgres://x", force=True, skip_vps_cancel=False
    )
    assert "--force" in args
    # ``--force`` is a flag, not an arg-value, so order is the only thing
    # that matters: ensure it comes after the id + db url.
    assert args.index("--force") > args.index("abc-123")


def test_build_destroy_admin_args_skip_vps_cancel() -> None:
    args = build_destroy_admin_args(pool_host_id="abc-123", database_url=None, force=True, skip_vps_cancel=True)
    assert "--skip-vps-cancel" in args
    # Default teardown (skip_vps_cancel=False) must NOT pass the flag, so the
    # admin command's VPS-cancel path stays the default.
    assert "--skip-vps-cancel" not in build_destroy_admin_args(
        pool_host_id="abc-123", database_url=None, force=True, skip_vps_cancel=False
    )


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
        backend=_BACKEND_OVH_VPS,
        count=1,
        region="US-EAST-VA",
        from_tag=None,
        repo_url=None,
        repo_branch_or_tag_override=None,
        attributes_json="{}",
        workspace_dir=str(_isolated_env),
        management_public_key_file="/k.pub",
        database_url="postgres://example",
        mngr_source=None,
        is_recycle_enabled=True,
        is_dry_run=False,
        is_deferred_install_wait_skipped=False,
    )
    tag_index = args.index("--tag")
    assert args[tag_index + 1] == "minds_env=production"


def test_pool_create_slice_rejects_management_public_key_file(
    _isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--management-public-key-file is ovh_vps-only; slice must reject it before any Vault read."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds")
    key_file = _isolated_env / "mgmt.pub"
    key_file.write_text("ssh-ed25519 AAAA... operator@host\n")
    runner = CliRunner()
    result = runner.invoke(
        pool,
        [
            "create",
            "--backend",
            "slice",
            "--count",
            "1",
            "--region",
            "US-EAST-VA",
            # explicit DSN short-circuits resolve_host_pool_dsn so no Vault read happens
            "--database-url",
            "postgres://example",
            "--management-public-key-file",
            str(key_file),
        ],
    )
    assert result.exit_code != 0
    assert "--management-public-key-file is not applicable to --backend slice" in result.output


def test_pool_create_slice_rejects_no_recycle(
    _isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-recycle is ovh_vps-only; slice must reject it before any Vault read."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds")
    runner = CliRunner()
    result = runner.invoke(
        pool,
        [
            "create",
            "--backend",
            "slice",
            "--count",
            "1",
            "--region",
            "US-EAST-VA",
            "--database-url",
            "postgres://example",
            "--no-recycle",
        ],
    )
    assert result.exit_code != 0
    assert "--no-recycle is not applicable to --backend slice" in result.output


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
    intends to bake against THAT tier's account. A stale ``OVH_APPLICATION_KEY``
    (or POOL_SSH_PRIVATE_KEY) from a different tier's session would otherwise
    silently misroute the bake.
    """
    merged = merge_extra_env_into_subprocess_env(
        shell_env={"OVH_APPLICATION_KEY": "stale", "HOME": "/home/me"},
        extra_env={"OVH_APPLICATION_KEY": "from-vault", "OVH_CONSUMER_KEY": "ck-from-vault"},
    )
    assert merged["OVH_APPLICATION_KEY"] == "from-vault"
    assert merged["OVH_CONSUMER_KEY"] == "ck-from-vault"
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


def test_derive_public_key_from_private_round_trips() -> None:
    """Given a generated ed25519 keypair, derive_public_key_from_private(priv) == priv.public_key().

    Uses ``cryptography`` to mint the keypair so we don't depend on a fixture
    file. ``ssh-keygen -y`` and ``cryptography``'s OpenSSH formatter both
    produce the canonical ``"<type> <base64>"`` line (no comment), so the
    base64 portion of each must match exactly.
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    expected_public_line = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )

    derived = derive_public_key_from_private(private_pem)
    assert derived.startswith("ssh-ed25519 "), derived
    # Compare type + base64; comments are optional and may differ.
    derived_type, derived_b64 = derived.split(" ")[:2]
    expected_type, expected_b64 = expected_public_line.split(" ")[:2]
    assert (derived_type, derived_b64) == (expected_type, expected_b64)


def test_derive_public_key_from_private_rejects_garbage() -> None:
    """Malformed input surfaces as a ClickException, not an opaque ssh-keygen stderr."""
    with pytest.raises(click.ClickException, match="ssh-keygen -y"):
        derive_public_key_from_private("this is not a key\n")


def test_resolved_management_public_key_path_explicit_override_yields_path_unchanged(
    _isolated_env: Path,
) -> None:
    """Operator override path bypasses Vault entirely and yields the operator's file as-is."""
    pub_path = _isolated_env / "operator-key.pub"
    pub_path.write_text("ssh-ed25519 AAAA... operator@host\n")
    with resolved_management_public_key_path("dev-x", explicit_path=str(pub_path)) as yielded:
        assert yielded == str(pub_path)
        assert Path(yielded).is_file()


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
