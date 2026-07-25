import os
import re
from pathlib import Path

import pytest

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MINDS_ROOT_NAME_PATTERN
from imbue.minds.bootstrap import apply_bootstrap
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_env_activated
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import root_name_for_env_name


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


def test_legacy_devminds_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale `MINDS_ROOT_NAME=devminds` parent shell fails loudly.

    Values that don't match `minds(-<env-name>)?` used to be coerced to production, which silently pointed tooling at production data; now they raise with the unset-then-activate fix.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    with pytest.raises(BootstrapError, match="unset MINDS_ROOT_NAME"):
        resolve_minds_root_name()


def test_value_with_spaces_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "Has Spaces")
    with pytest.raises(BootstrapError):
        resolve_minds_root_name()


def test_path_with_dot_dot_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "../evil")
    with pytest.raises(BootstrapError):
        resolve_minds_root_name()


def test_is_active_when_set_to_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-josh-3")
    assert is_env_activated() is True


def test_is_active_when_set_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds")
    assert is_env_activated() is True


def test_is_active_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert is_env_activated() is False


def test_is_active_raises_for_legacy_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale shell with `MINDS_ROOT_NAME=devminds` fails loudly rather than reading as unactivated."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    with pytest.raises(BootstrapError):
        is_env_activated()


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


def test_apply_bootstrap_invalid_value_raises_and_leaves_vars_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale `MINDS_ROOT_NAME=devminds` shell fails loudly instead of exporting production paths."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "devminds")
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    with pytest.raises(BootstrapError):
        apply_bootstrap()
    assert os.environ["MNGR_HOST_DIR"] == "/custom/host/dir"


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
