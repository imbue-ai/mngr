import os
import re
import tomllib
from pathlib import Path

import pytest

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MINDS_ROOT_NAME_PATTERN
from imbue.minds.bootstrap import _ensure_mngr_settings
from imbue.minds.bootstrap import apply_bootstrap
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_minds_root_name_set_to_active_env
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.bootstrap import set_imbue_cloud_provider_for_account
from imbue.minds.bootstrap import set_provider_is_enabled
from imbue.minds.primitives import CONFIGURED_AWS_REGIONS
from imbue.minds.testing import stub_mngr_host_dir


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove MINDS_ROOT_NAME and MNGR_* overrides that tests might have set."""
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR, raising=False)
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    monkeypatch.delenv("MNGR_PREFIX", raising=False)


def test_defaults_to_minds_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_accepts_minds_value_for_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds")
    assert resolve_minds_root_name() == "minds"


def test_accepts_minds_prefix_for_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-josh-3")
    assert resolve_minds_root_name() == "minds-dev-josh-3"


def test_accepts_minds_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    assert resolve_minds_root_name() == "minds-staging"


def test_legacy_devminds_value_falls_back_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale `MINDS_ROOT_NAME=devminds` parent shell shouldn't break us.

    Per the per-env-data-roots refactor: values that don't match the
    `minds(-<env-name>)?` pattern are silently treated as unset and the
    caller falls back to production (~/.minds/). The Python warning
    surfaces in logs so the operator notices.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    # No SystemExit -- just fall back to the default.
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_legacy_value_with_spaces_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "Has Spaces")
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_path_with_dot_dot_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "../evil")
    # ../evil cannot match `minds(-<env-name>)?` -- treated as unset.
    assert resolve_minds_root_name() == DEFAULT_MINDS_ROOT_NAME


def test_is_active_when_set_to_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-josh-3")
    assert is_minds_root_name_set_to_active_env() is True


def test_is_active_when_set_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds")
    assert is_minds_root_name_set_to_active_env() is True


def test_is_active_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert is_minds_root_name_set_to_active_env() is False


def test_is_active_false_for_legacy_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale shell with `MINDS_ROOT_NAME=devminds` is NOT activated.

    `minds env deploy` / `destroy` use this distinction to refuse safely
    even when the bootstrap fallback would still produce a usable host_dir.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    assert is_minds_root_name_set_to_active_env() is False


def test_env_name_from_root_name_production() -> None:
    assert env_name_from_root_name("minds") == "production"


def test_env_name_from_root_name_dev() -> None:
    assert env_name_from_root_name("minds-dev-josh-3") == "dev-josh-3"


def test_env_name_from_root_name_staging() -> None:
    assert env_name_from_root_name("minds-staging") == "staging"


def test_env_name_from_root_name_rejects_garbage() -> None:
    with pytest.raises(BootstrapError):
        env_name_from_root_name("devminds")


def test_root_name_for_env_name_production() -> None:
    assert root_name_for_env_name("production") == "minds"


def test_root_name_for_env_name_dev() -> None:
    assert root_name_for_env_name("dev-josh-3") == "minds-dev-josh-3"


def test_root_name_for_env_name_staging() -> None:
    assert root_name_for_env_name("staging") == "minds-staging"


def test_minds_data_dir_for() -> None:
    assert minds_data_dir_for("minds-dev-josh-3") == Path.home() / ".minds-dev-josh-3"
    assert minds_data_dir_for("minds") == Path.home() / ".minds"


def test_mngr_host_dir_for() -> None:
    assert mngr_host_dir_for("minds-dev-josh-3") == Path.home() / ".minds-dev-josh-3" / "mngr"


def test_mngr_prefix_for() -> None:
    assert mngr_prefix_for("minds-dev-josh-3") == "minds-dev-josh-3-"
    assert mngr_prefix_for("minds") == "minds-"


def test_apply_bootstrap_sets_env_vars_when_root_name_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-testname")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".minds-dev-testname" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-dev-testname-"


def test_apply_bootstrap_overrides_inherited_mngr_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit MINDS_ROOT_NAME wins over an inherited MNGR_HOST_DIR/MNGR_PREFIX.

    Without this, a minds process spawned from a parent that already set
    MNGR_HOST_DIR (e.g. a Claude Code agent's tmux) would silently keep the
    parent's host_dir and read a different mngr settings.toml than the one
    minds bootstrap writes to.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-josh-3")
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    monkeypatch.setenv("MNGR_PREFIX", "custom-")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".minds-dev-josh-3" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-dev-josh-3-"


def test_apply_bootstrap_leaves_mngr_vars_alone_when_root_name_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-env-data-roots refactor: apply_bootstrap is a no-op when MINDS_ROOT_NAME is unset.

    Callers that need an activated env refuse explicitly. Callers that
    only need the production data dir handle it themselves.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    monkeypatch.setenv("MNGR_PREFIX", "custom-")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == "/custom/host/dir"
    assert os.environ["MNGR_PREFIX"] == "custom-"


def test_apply_bootstrap_unset_does_not_write_mngr_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    apply_bootstrap()
    # Vars stay unset because there's no activated env to drive them.
    assert "MNGR_HOST_DIR" not in os.environ
    assert "MNGR_PREFIX" not in os.environ


def test_apply_bootstrap_invalid_value_still_writes_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale `MINDS_ROOT_NAME=devminds` shell still gets consistent MNGR_* vars.

    The bootstrap resolves to the production default and exports the
    derived vars so downstream mngr calls have *some* consistent
    host_dir to point at instead of half-honoring the bad value.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    apply_bootstrap()
    assert os.environ["MNGR_HOST_DIR"] == str(Path.home() / ".minds" / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-"


def test_minds_root_name_pattern_canonical_examples() -> None:
    """Sanity-check the regex's expectations directly."""
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-staging") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-dev-josh-3") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-dev-tname") is not None
    # CI ephemeral envs (minted by the deployment-tests orchestrator)
    # share the same shape as dev envs but with a ``ci-`` prefix.
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-ci-20260518t140212z") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-ci-20260518t140212z-abcd") is not None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "devminds") is None
    # Bare `minds-` with no suffix is rejected -- the env-name regex
    # forbids an empty suffix.
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-") is None
    # Single-char env-name suffixes are rejected -- DEV_ENV_NAME_PATTERN
    # requires both a leading and a trailing alphanumeric (2+ chars).
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-a") is None
    # Dynamic envs MUST lead with ``dev-`` or ``ci-``; anything else
    # under the prefix is rejected as not matching either the staging
    # or dynamic-env shape.
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-josh-3") is None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-josh") is None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-production") is None
    # Bare ``dev-`` / ``ci-`` with nothing after is rejected (the
    # suffix needs 2+ chars of [a-z0-9_-]).
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-dev-") is None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-dev-a") is None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-ci-") is None
    assert re.fullmatch(MINDS_ROOT_NAME_PATTERN, "minds-ci-a") is None


_FAKE_CONNECTOR_URL = "https://test--rsc-api.modal.run"


def test_set_imbue_cloud_provider_for_account_writes_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-dev-tname",
    )
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    block = parsed["providers"]["imbue_cloud_alice-example-com"]
    assert block == {
        "backend": "imbue_cloud",
        "account": "alice@example.com",
        "connector_url": _FAKE_CONNECTOR_URL,
        "is_enabled": True,
        # Runsc + hardening args so the slow (rebuild) path runs under gVisor.
        "docker_runtime": "runsc",
        "install_gvisor_runtime": True,
        "default_start_args": ["--workdir=/", "--security-opt=no-new-privileges"],
    }


def test_set_provider_is_enabled_flips_is_enabled_on_existing_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-dev-tname",
    )

    changed = set_provider_is_enabled("imbue_cloud_alice-example-com", False, root_name="minds-dev-tname")
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is False

    # Idempotent: setting to the same value is a no-op.
    assert set_provider_is_enabled("imbue_cloud_alice-example-com", False, root_name="minds-dev-tname") is False

    # Re-enabling flips the bit back.
    assert set_provider_is_enabled("imbue_cloud_alice-example-com", True, root_name="minds-dev-tname") is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is True


def test_set_provider_is_enabled_creates_override_block_for_missing_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When [providers.<name>] doesn't exist in minds' settings, it's created with just is_enabled."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")

    changed = set_provider_is_enabled("docker", False, root_name="minds-dev-tname")
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["docker"] == {"is_enabled": False}

    # Now re-enable: same block is updated.
    changed = set_provider_is_enabled("docker", True, root_name="minds-dev-tname")
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["docker"] == {"is_enabled": True}


def test_set_provider_is_enabled_creates_settings_file_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If minds' active settings file does not yet exist, it is created."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    # Make sure no file exists yet
    if settings_path.exists():
        settings_path.unlink()

    changed = set_provider_is_enabled("modal", False, root_name="minds-dev-tname")
    assert changed is True
    assert settings_path.exists()
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["modal"] == {"is_enabled": False}


def test_set_force_enable_re_enables_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-dev-tname",
    )
    set_provider_is_enabled("imbue_cloud_alice-example-com", False, root_name="minds-dev-tname")

    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-dev-tname",
        force_enable=True,
    )
    assert changed is True
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is True


def test_set_preserve_does_not_re_enable_disabled_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bootstrap reconcile path must leave a previously disabled
    provider (e.g. from the providers panel toggle) disabled -- only an
    explicit signin event force-enables.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-dev-tname",
    )
    set_provider_is_enabled("imbue_cloud_alice-example-com", False, root_name="minds-dev-tname")

    changed = set_imbue_cloud_provider_for_account(
        "alice@example.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-dev-tname",
        force_enable=False,
    )
    assert changed is False
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud_alice-example-com"]["is_enabled"] is False


def test_ensure_mngr_settings_writes_default_imbue_cloud_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_ensure_mngr_settings`` must suppress the default ``[providers.imbue_cloud]``
    instance so ``get_all_provider_instances`` doesn't auto-create one alongside
    the per-account ``imbue_cloud_<slug>`` entries.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    _ensure_mngr_settings("minds-dev-tname")
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False


def test_ensure_mngr_settings_writes_default_aws_disabled_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The region-less default ``[providers.aws]`` instance must be suppressed even with no AWS creds.

    Otherwise ``get_all_provider_instances`` auto-creates it and its discovery
    fails every ``mngr list`` cycle ("credentials not configured"), logging a
    spurious warning. This is the no-credentials case, where no per-region
    ``aws-<region>`` blocks are written, so the default would be the only AWS
    provider present.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    _ensure_mngr_settings("minds-dev-tname")
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["aws"] == {"backend": "aws", "is_enabled": False}
    assert not [name for name in parsed["providers"] if name.startswith("aws-")]


def test_ensure_mngr_settings_keeps_default_aws_disabled_alongside_region_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default ``[providers.aws]`` stays suppressed even when per-region blocks are written."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    _ensure_mngr_settings("minds-dev-tname")
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["aws"] == {"backend": "aws", "is_enabled": False}
    assert [name for name in parsed["providers"] if name.startswith("aws-")]


def test_ensure_mngr_settings_writes_aws_blocks_when_credentials_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One ``[providers.aws-<region>]`` block is written per configured region when AWS creds exist."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    _ensure_mngr_settings("minds-dev-tname")
    parsed = tomllib.loads(settings_path.read_text())
    providers = parsed["providers"]
    for region in CONFIGURED_AWS_REGIONS:
        block = providers[f"aws-{region}"]
        assert block == {
            "backend": "aws",
            "default_region": region,
            "default_instance_type": "t3.large",
            "install_gvisor_runtime": True,
            "docker_runtime": "runsc",
        }


def test_ensure_mngr_settings_omits_aws_blocks_when_no_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No AWS provider blocks are written when no AWS credentials are configured.

    Writing dead blocks would make ``mngr list`` fan out to AWS providers that
    can't authenticate, logging a provider-unavailable error per region.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    _ensure_mngr_settings("minds-dev-tname")
    parsed = tomllib.loads(settings_path.read_text())
    assert not [name for name in parsed["providers"] if name.startswith("aws-")]


def test_ensure_mngr_settings_removes_stale_aws_blocks_when_credentials_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stale ``aws-<region>`` blocks are pruned once AWS credentials are no longer present."""
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    _ensure_mngr_settings("minds-dev-tname")
    parsed = tomllib.loads(settings_path.read_text())
    assert [name for name in parsed["providers"] if name.startswith("aws-")]

    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    _ensure_mngr_settings("minds-dev-tname")
    parsed_after = tomllib.loads(settings_path.read_text())
    assert not [name for name in parsed_after["providers"] if name.startswith("aws-")]


def test_set_imbue_cloud_provider_for_account_also_writes_default_disabled_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: a first signin on a fresh ``MINDS_ROOT_NAME`` must land
    both the per-account block AND the default-disabled
    ``[providers.imbue_cloud]`` suppression block.

    Without the suppression block, ``mngr observe`` auto-creates a phantom
    default ``imbue_cloud`` instance with no ``connector_url``, which
    raises ``MissingConnectorUrlError`` on every discovery cycle and
    breaks ``mngr create`` against the env. ``apply_bootstrap``'s call
    to ``_ensure_mngr_settings`` no-ops on a fresh env (the mngr profile
    dir doesn't exist yet at startup), so ``set_imbue_cloud_provider_for_account``
    has to ensure it as part of writing the per-account block.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-staging")
    set_imbue_cloud_provider_for_account(
        "josh@imbue.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-staging",
    )
    parsed = tomllib.loads(settings_path.read_text())
    # The per-account block lands as before.
    assert parsed["providers"]["imbue_cloud_josh-imbue-com"]["connector_url"] == _FAKE_CONNECTOR_URL
    # AND the suppression block + recursive-disable land in the same pass.
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False


def test_set_imbue_cloud_provider_for_account_repairs_missing_default_block_on_resignin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An already-signed-in user whose settings.toml is missing the
    suppression block (because the original signin happened on a build
    that didn't write it) gets the block back on the next signin event,
    even when the per-account block itself is unchanged and the function
    short-circuits its per-account write path.
    """
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-staging")
    # Pre-seed: a per-account block exists but the suppression block is missing
    # (mirrors the on-disk state of a staging env that signed in before the
    # fix landed).
    settings_path.write_text(
        "[providers.imbue_cloud_josh-imbue-com]\n"
        'backend = "imbue_cloud"\n'
        'account = "josh@imbue.com"\n'
        f'connector_url = "{_FAKE_CONNECTOR_URL}"\n'
        "is_enabled = true\n"
        'docker_runtime = "runsc"\n'
        "install_gvisor_runtime = true\n"
        'default_start_args = ["--workdir=/", "--security-opt=no-new-privileges"]\n'
    )

    changed = set_imbue_cloud_provider_for_account(
        "josh@imbue.com",
        connector_url=_FAKE_CONNECTOR_URL,
        root_name="minds-staging",
    )
    # The per-account write itself is a no-op (existing block already matches),
    # but the file is still modified because the suppression block lands.
    assert changed is False
    parsed = tomllib.loads(settings_path.read_text())
    assert parsed["providers"]["imbue_cloud"] == {"backend": "imbue_cloud", "is_enabled": False}
    assert parsed["plugins"]["recursive"]["enabled"] is False
