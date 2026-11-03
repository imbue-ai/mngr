import json
import os
import re
from pathlib import Path

import pytest

from imbue.minds.bootstrap import BootstrapError
from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_APP_NAME
from imbue.minds.bootstrap import MINDS_CACHE_DIR_ENV_VAR
from imbue.minds.bootstrap import MINDS_LOG_DIR_ENV_VAR
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import MINDS_ROOT_NAME_PATTERN
from imbue.minds.bootstrap import MINDS_STATE_DIR_ENV_VAR
from imbue.minds.bootstrap import MindsPathRole
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.bootstrap import STUDIO_ROOTS_OVERRIDE_ENV_VAR
from imbue.minds.bootstrap import XDG_APP_DIR_NAME
from imbue.minds.bootstrap import apply_bootstrap
from imbue.minds.bootstrap import check_legacy_dotfolder_has_migrated
from imbue.minds.bootstrap import default_root_name_to_production
from imbue.minds.bootstrap import env_name_from_root_name
from imbue.minds.bootstrap import is_env_activated
from imbue.minds.bootstrap import list_state_tier_names
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import minds_dir_for_role
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import root_name_for_env_name


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave MINDS_ROOT_NAME, the MNGR_* overrides, the minds path overrides (including the retired MINDS_DATA_HOME) and the XDG base directories unset, restoring at teardown.

    monkeypatch only records vars it changed itself, so a plain delenv of an absent var would leave
    anything the code under test writes directly into os.environ (apply_bootstrap does) in place after
    the test. Setting a placeholder first makes it remember the original (absent) state.
    """
    for name in (
        MINDS_ROOT_NAME_ENV_VAR,
        "MNGR_HOST_DIR",
        "MNGR_PREFIX",
        STUDIO_ROOTS_OVERRIDE_ENV_VAR,
        "MINDS_DATA_HOME",
        MINDS_STATE_DIR_ENV_VAR,
        MINDS_CACHE_DIR_ENV_VAR,
        MINDS_LOG_DIR_ENV_VAR,
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


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


def test_default_root_name_to_production_seeds_minds_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    default_root_name_to_production()
    assert os.environ[MINDS_ROOT_NAME_ENV_VAR] == DEFAULT_MINDS_ROOT_NAME
    apply_bootstrap()
    assert Path(os.environ["MNGR_HOST_DIR"]) == mngr_host_dir_for(DEFAULT_MINDS_ROOT_NAME)
    assert os.environ["MNGR_PREFIX"] == mngr_prefix_for(DEFAULT_MINDS_ROOT_NAME)


@pytest.mark.parametrize("root_name", ["minds-staging", "minds-dev-josh-3", "devminds"])
def test_default_root_name_to_production_leaves_a_set_value_alone(
    monkeypatch: pytest.MonkeyPatch, root_name: str
) -> None:
    """An activated env, and even a stale invalid value, must reach the bootstrap untouched so its own handling applies."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, root_name)
    default_root_name_to_production()
    assert os.environ[MINDS_ROOT_NAME_ENV_VAR] == root_name


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


def test_mngr_host_dir_for(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """mngr's host dir hangs off the tier's state root, not the legacy dotfolder.

    Resolved through ``STUDIO_ROOTS_OVERRIDE`` so the assertion holds on every platform: this helper
    takes no ``platform`` argument, and the roots have no answer off darwin. What the Apple
    layout spells the state root is asserted by
    :func:`test_darwin_roots_are_the_apple_canonical_three`, which can name a platform.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path))
    assert (
        mngr_host_dir_for("minds-dev-josh-3") == minds_dir_for_role(MindsPathRole.STATE, "minds-dev-josh-3") / "mngr"
    )
    assert mngr_host_dir_for("minds-dev-josh-3") == tmp_path / "dev-josh-3" / "state" / "mngr"


def test_mngr_prefix_for() -> None:
    assert mngr_prefix_for("minds-dev-josh-3") == "minds-dev-josh-3-"
    assert mngr_prefix_for("minds") == "minds-"


def test_apply_bootstrap_sets_env_vars_when_root_name_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-testname")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(minds_dir_for_role(MindsPathRole.STATE, "minds-dev-testname") / "mngr")
    assert os.environ["MNGR_PREFIX"] == "minds-dev-testname-"


def test_apply_bootstrap_overrides_inherited_mngr_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Explicit MINDS_ROOT_NAME wins over an inherited MNGR_HOST_DIR/MNGR_PREFIX.

    Without this, an Imbue Studio process spawned from a parent that already set
    MNGR_HOST_DIR (e.g. a Claude Code agent's tmux) would silently keep the
    parent's host_dir and read a different mngr settings.toml than the one
    Imbue Studio bootstrap writes to.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path))
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-josh-3")
    monkeypatch.setenv("MNGR_HOST_DIR", "/custom/host/dir")
    monkeypatch.setenv("MNGR_PREFIX", "custom-")
    apply_bootstrap()

    assert os.environ["MNGR_HOST_DIR"] == str(minds_dir_for_role(MindsPathRole.STATE, "minds-dev-josh-3") / "mngr")
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


def test_minds_app_name_matches_the_electron_product_name() -> None:
    """The segment the Apple roots are keyed on exists three times over, and a drift splits the app in half.

    ``electron/platform-roots.js`` pins its own copy against the same ``productName``, so anchoring both halves here is what stops the shell writing a tier's state to one directory while the backend reads another.
    """
    package_json = Path(__file__).resolve().parents[2] / "package.json"
    assert MINDS_APP_NAME == json.loads(package_json.read_text())["productName"]


def test_xdg_app_dir_name_matches_the_package_name_and_carries_no_space() -> None:
    """The XDG roots are keyed on the package name, not the display name, and ``electron/platform-roots.js`` pins its own copy against the same field.

    The space in ``productName`` is what makes it wrong here: every path below these roots is interpolated into shell and command lines.
    """
    package_json = Path(__file__).resolve().parents[2] / "package.json"
    assert XDG_APP_DIR_NAME == json.loads(package_json.read_text())["name"]
    assert not any(character.isspace() for character in XDG_APP_DIR_NAME)


def _darwin_roots(root_name: str) -> dict[MindsPathRole, Path]:
    return {role: minds_dir_for_role(role, root_name, platform="darwin") for role in MindsPathRole}


def _expected_apple_roots(tier: str) -> dict[MindsPathRole, Path]:
    """The three roots Apple's File System Programming Guide names, spelled out independently of the resolver."""
    library = Path.home() / "Library"
    return {
        MindsPathRole.STATE: library / "Application Support" / MINDS_APP_NAME / tier,
        MindsPathRole.CACHE: library / "Caches" / MINDS_APP_NAME / tier,
        MindsPathRole.LOGS: library / "Logs" / MINDS_APP_NAME / tier,
    }


def test_darwin_roots_are_the_apple_canonical_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the layout: three distinct Apple-canonical roots, tier-suffixed.

    Caches and Logs are the roots Time Machine excludes by policy, so filing regenerable files under them is what actually keeps them out of backups.
    """
    _clear_env(monkeypatch)
    assert _darwin_roots(DEFAULT_MINDS_ROOT_NAME) == _expected_apple_roots("production")


def test_every_role_resolves_to_a_distinct_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    roots = _darwin_roots(DEFAULT_MINDS_ROOT_NAME)
    assert len(set(roots.values())) == len(MindsPathRole)


def test_xdg_variables_do_not_move_the_darwin_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A developer's XDG variables must not move the backend off the roots the Electron shell hardcodes.

    ``platformdirs``' macOS class honors ``$XDG_DATA_HOME`` / ``$XDG_CACHE_HOME`` but has no XDG counterpart for the log dir, so resolving through it would send state and cache somewhere ``electron/platform-roots.js`` never looks while leaving logs behind on ``~/Library/Logs`` -- one tier split across two layouts.
    """
    _clear_env(monkeypatch)
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    assert _darwin_roots(DEFAULT_MINDS_ROOT_NAME) == _expected_apple_roots("production")


def _expected_xdg_roots(tier: str) -> dict[MindsPathRole, Path]:
    home = Path.home()
    return {
        MindsPathRole.STATE: home / ".local" / "share" / XDG_APP_DIR_NAME / tier,
        MindsPathRole.CACHE: home / ".cache" / XDG_APP_DIR_NAME / tier,
        MindsPathRole.LOGS: home / ".local" / "state" / XDG_APP_DIR_NAME / tier / "log",
    }


def _linux_roots(root_name: str) -> dict[MindsPathRole, Path]:
    return {role: minds_dir_for_role(role, root_name, platform="linux") for role in MindsPathRole}


def test_off_darwin_each_role_resolves_to_its_own_xdg_base_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing the cache to its own base directory is what gets the regenerable bytes excluded: backup tools skip the cache directory and nothing excluded the dotfolder."""
    _clear_env(monkeypatch)
    assert _linux_roots(DEFAULT_MINDS_ROOT_NAME) == _expected_xdg_roots("production")


def test_off_darwin_the_tier_is_a_segment_below_each_role_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dotfolder carried the tier in its own name; here two tiers are two subdirectories, so one tier's roots can never nest inside another's."""
    _clear_env(monkeypatch)
    staging = minds_dir_for_role(MindsPathRole.STATE, "minds-staging", platform="linux")
    production = minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME, platform="linux")
    assert staging == Path.home() / ".local" / "share" / XDG_APP_DIR_NAME / "staging"
    assert staging != production
    assert production not in staging.parents


def test_off_darwin_the_xdg_variables_are_honored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A host that exports these expects every writer to follow them, and the Electron shell reads the same three."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    assert _linux_roots(DEFAULT_MINDS_ROOT_NAME) == {
        MindsPathRole.STATE: tmp_path / "d" / XDG_APP_DIR_NAME / "production",
        MindsPathRole.CACHE: tmp_path / "c" / XDG_APP_DIR_NAME / "production",
        MindsPathRole.LOGS: tmp_path / "s" / XDG_APP_DIR_NAME / "production" / "log",
    }


@pytest.mark.parametrize("value", ["", "   ", "rel/ative"])
def test_an_empty_or_relative_xdg_variable_falls_back_to_its_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The spec reads empty as absent and requires a relative path to be ignored; honoring one would put a tier's credentials somewhere relative to the cwd."""
    _clear_env(monkeypatch)
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, value)
    assert _linux_roots(DEFAULT_MINDS_ROOT_NAME) == _expected_xdg_roots("production")


def test_off_darwin_the_shell_and_the_backend_name_the_same_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """electron/platform-roots.js resolves these same three off darwin; a disagreement would split a tier's files between the two runtimes."""
    _clear_env(monkeypatch)
    state = minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME, platform="linux")
    cache = minds_dir_for_role(MindsPathRole.CACHE, DEFAULT_MINDS_ROOT_NAME, platform="linux")
    logs = minds_dir_for_role(MindsPathRole.LOGS, DEFAULT_MINDS_ROOT_NAME, platform="linux")
    state_root = Path.home() / ".local" / "share" / XDG_APP_DIR_NAME / "production"
    assert (state / "mngr") == state_root / "mngr"
    assert (state / ".venv") == state_root / ".venv"
    assert (cache / ".uv-cache") == Path.home() / ".cache" / XDG_APP_DIR_NAME / "production" / ".uv-cache"
    assert logs == Path.home() / ".local" / "state" / XDG_APP_DIR_NAME / "production" / "log"


def test_the_legacy_dotfolder_is_not_a_live_root_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """``minds_data_dir_for`` is only the migration's source, which is what gives a Linux launch a real source and destination rather than one directory."""
    _clear_env(monkeypatch)
    legacy = minds_data_dir_for(DEFAULT_MINDS_ROOT_NAME)
    assert legacy == Path.home() / ".minds"
    assert legacy not in _linux_roots(DEFAULT_MINDS_ROOT_NAME).values()


def test_tiers_are_enumerated_from_the_shared_parent_off_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``minds-admin env list`` works off darwin, where the tier is now a directory below the state parent as it is on darwin."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    state_parent = tmp_path / ".local" / "share" / XDG_APP_DIR_NAME
    for tier in ("production", "staging", "dev-josh-3"):
        (state_parent / tier).mkdir(parents=True)
    assert sorted(list_state_tier_names(platform="linux")) == ["dev-josh-3", "production", "staging"]


def test_tier_enumeration_off_darwin_ignores_an_unmigrated_dotfolder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tier whose files have not moved off ``~/.minds`` has nothing under the state parent, and the emptied husk left behind must not read as one either."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".minds-staging").mkdir()
    (tmp_path / ".local" / "share" / XDG_APP_DIR_NAME / "production").mkdir(parents=True)
    assert list_state_tier_names(platform="linux") == ("production",)


def test_tiers_are_enumerated_from_the_shared_parent_on_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On darwin the tier is a directory below the state root, so the dotfolders are not consulted at all."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    state_parent = tmp_path / "Library" / "Application Support" / MINDS_APP_NAME
    (state_parent / "production").mkdir(parents=True)
    (state_parent / "staging").mkdir()
    (tmp_path / ".minds-dev-only-a-dotfolder").mkdir()
    assert sorted(list_state_tier_names(platform="darwin")) == ["production", "staging"]


def test_injected_roots_cannot_answer_for_every_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An injected root is resolved for the one tier this process was launched as, so it cannot name where every tier lives."""
    _clear_env(monkeypatch)
    _set_injected_roots(monkeypatch, tmp_path)
    with pytest.raises(BootstrapError, match="resolved for one tier"):
        list_state_tier_names(platform="darwin")


@pytest.mark.parametrize(
    ("root_name", "expected_tier"),
    (
        ("minds", "production"),
        ("minds-staging", "staging"),
        ("minds-dev-josh-3", "dev-josh-3"),
        ("minds-ci-20260518t140212z-abcd", "ci-20260518t140212z-abcd"),
    ),
)
def test_tier_is_the_first_subdirectory_of_each_root(
    monkeypatch: pytest.MonkeyPatch, root_name: str, expected_tier: str
) -> None:
    _clear_env(monkeypatch)
    assert MindsRoot(root_name).tier == expected_tier
    for root in _darwin_roots(root_name).values():
        assert root.name == expected_tier


def test_staging_and_dev_staging_do_not_collide(monkeypatch: pytest.MonkeyPatch) -> None:
    """``minds-staging`` and ``minds-dev-staging`` are different tiers and must stay on different roots.

    They are distinguished today only by the folder-name suffix in ``$HOME``; a prefix-stripping slip while lifting the tier into a subdirectory would silently merge a developer's env into shared staging.
    """
    _clear_env(monkeypatch)
    staging = _darwin_roots("minds-staging")
    dev_staging = _darwin_roots("minds-dev-staging")
    assert MindsRoot("minds-staging").tier == "staging"
    assert MindsRoot("minds-dev-staging").tier == "dev-staging"
    for role in MindsPathRole:
        assert staging[role] != dev_staging[role]


def test_roots_override_collects_all_roots_under_one_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``STUDIO_ROOTS_OVERRIDE`` is what keeps a CI run self-contained, writing to a throwaway directory instead of the roots the runner's own install owns."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path))
    assert minds_dir_for_role(MindsPathRole.STATE, "minds-staging") == tmp_path / "staging" / "state"
    assert minds_dir_for_role(MindsPathRole.CACHE, "minds-staging") == tmp_path / "staging" / "cache"
    assert minds_dir_for_role(MindsPathRole.LOGS, "minds-staging") == tmp_path / "staging" / "logs"


def test_roots_override_outranks_the_xdg_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The override is consulted before the platform dispatch, so a caller that wants an isolated root gets one on every platform -- including a host that exports the XDG variables."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path))
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "ignored" / name.lower()))
    resolved = minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME, platform="linux")
    assert resolved == tmp_path / "production" / "state"


def _set_injected_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(MINDS_STATE_DIR_ENV_VAR, str(tmp_path / "s"))
    monkeypatch.setenv(MINDS_CACHE_DIR_ENV_VAR, str(tmp_path / "c"))
    monkeypatch.setenv(MINDS_LOG_DIR_ENV_VAR, str(tmp_path / "l"))


def test_injected_roots_are_honored_verbatim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The backend consumes the roots the Electron shell resolved instead of recomputing them.

    This is the invariant that replaces a cross-runtime agreement test: the shell owns resolution (it creates the virtualenv before any Python exists to ask), so the two cannot disagree.
    """
    _clear_env(monkeypatch)
    _set_injected_roots(monkeypatch, tmp_path)
    assert minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME) == tmp_path / "s"
    assert minds_dir_for_role(MindsPathRole.CACHE, DEFAULT_MINDS_ROOT_NAME) == tmp_path / "c"
    assert minds_dir_for_role(MindsPathRole.LOGS, DEFAULT_MINDS_ROOT_NAME) == tmp_path / "l"


def test_injected_roots_are_honored_on_an_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once the shell has resolved the layout the backend needs no opinion of its own, so the platform guard must not fire."""
    _clear_env(monkeypatch)
    _set_injected_roots(monkeypatch, tmp_path)
    assert minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME, platform="linux") == tmp_path / "s"


def test_injected_roots_beat_the_roots_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The shell already applied ``STUDIO_ROOTS_OVERRIDE`` when it resolved the roots, so re-applying it here would point the backend somewhere the shell is not looking."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path / "ignored"))
    _set_injected_roots(monkeypatch, tmp_path)
    assert minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME) == tmp_path / "s"


def test_injected_roots_answer_for_the_tier_the_process_was_launched_as(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tier the injecting parent resolved for is the tier the injected roots are valid for."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    _set_injected_roots(monkeypatch, tmp_path)
    assert minds_dir_for_role(MindsPathRole.STATE, "minds-staging") == tmp_path / "s"


def test_injected_roots_refuse_to_answer_for_another_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Injected roots are already tier-qualified, so reusing them for a second tier would return the first tier's directory -- the collapse the tier subdirectory exists to prevent.

    Falling back to the platform default for the odd tier out is the same split seen from the other side, so neither answer is available and it raises. Cross-tier resolution is a real caller shape: ``envs/paths.py`` resolves a directory per env name, not per active env.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-staging")
    _set_injected_roots(monkeypatch, tmp_path)
    with pytest.raises(BootstrapError, match="cannot answer for another tier"):
        minds_dir_for_role(MindsPathRole.STATE, "minds-dev-staging")


@pytest.mark.parametrize("requested_role", tuple(MindsPathRole))
def test_partially_injected_roots_raise_for_every_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, requested_role: MindsPathRole
) -> None:
    """A partial set would split one tier's files across two layouts, so it fails loudly rather than filling the gaps in from the platform default.

    Including -- especially -- when the role asked for is one of the missing ones: falling back for just that role is the split this rejects.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv(MINDS_STATE_DIR_ENV_VAR, str(tmp_path / "s"))
    with pytest.raises(BootstrapError, match=MINDS_CACHE_DIR_ENV_VAR):
        minds_dir_for_role(requested_role, DEFAULT_MINDS_ROOT_NAME)


def test_an_empty_injected_root_counts_as_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An exported-but-empty root is a launcher bug, and resolving it as an empty path or falling back for that one role both split the tier."""
    _clear_env(monkeypatch)
    _set_injected_roots(monkeypatch, tmp_path)
    monkeypatch.setenv(MINDS_LOG_DIR_ENV_VAR, "")
    with pytest.raises(BootstrapError, match=MINDS_LOG_DIR_ENV_VAR):
        minds_dir_for_role(MindsPathRole.LOGS, DEFAULT_MINDS_ROOT_NAME)


def test_legacy_data_dir_still_points_at_the_dotfolder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration reads from here; it must keep resolving to the pre-migration location whatever the new roots are."""
    _clear_env(monkeypatch)
    root = MindsRoot("minds-dev-josh-3")
    assert root.legacy_data_dir == minds_data_dir_for("minds-dev-josh-3")
    assert root.legacy_data_dir == Path.home() / ".minds-dev-josh-3"


def test_minds_root_exposes_each_role(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The accessor callers actually use, so it must route through the same resolution the free function does."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path))
    root = MindsRoot("minds-staging")
    assert root.state_dir == tmp_path / "staging" / "state"
    assert root.cache_dir == tmp_path / "staging" / "cache"
    assert root.log_dir == tmp_path / "staging" / "logs"


def test_the_retired_override_name_raises_rather_than_resolving_the_real_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller that still exports it believes it is isolated, so resolving the real roots behind it is the one outcome worse than stopping."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MINDS_DATA_HOME", str(tmp_path))
    with pytest.raises(BootstrapError, match=STUDIO_ROOTS_OVERRIDE_ENV_VAR):
        minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME, platform="linux")
    with pytest.raises(BootstrapError, match=STUDIO_ROOTS_OVERRIDE_ENV_VAR):
        list_state_tier_names(platform="linux")


def test_the_retired_name_is_ignored_once_the_current_one_is_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both set is what a shell looks like mid-rename, and the current name answers."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("MINDS_DATA_HOME", "/stale")
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path))
    assert minds_dir_for_role(MindsPathRole.STATE, DEFAULT_MINDS_ROOT_NAME, platform="linux") == (
        tmp_path / "production" / "state"
    )


def _unmigrated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at a tier that still has its dotfolder, and the roots at a throwaway. Returns the state root."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, DEFAULT_MINDS_ROOT_NAME)
    monkeypatch.setenv(STUDIO_ROOTS_OVERRIDE_ENV_VAR, str(tmp_path / "roots"))
    (tmp_path / f".{DEFAULT_MINDS_ROOT_NAME}").mkdir()
    return tmp_path / "roots" / "production" / "state"


def test_an_unmigrated_dotfolder_refuses_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The entries this process would create are the ones the shell's migration stops on."""
    state_dir = _unmigrated_home(monkeypatch, tmp_path)
    with pytest.raises(BootstrapError, match=re.escape(str(state_dir))):
        check_legacy_dotfolder_has_migrated()


def test_a_migrated_dotfolder_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_dir = _unmigrated_home(monkeypatch, tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / ".migrated-from-dotfolder").write_text("migrated 7 entries\n")
    check_legacy_dotfolder_has_migrated()


def test_no_dotfolder_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fresh install has nothing to move, so there is no destination to protect."""
    _unmigrated_home(monkeypatch, tmp_path)
    (tmp_path / f".{DEFAULT_MINDS_ROOT_NAME}").rmdir()
    check_legacy_dotfolder_has_migrated()


def test_injected_roots_run_against_an_unmigrated_dotfolder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The shell migrates before it spawns the backend, so its own child must not second-guess it."""
    state_dir = _unmigrated_home(monkeypatch, tmp_path)
    monkeypatch.setenv(MINDS_STATE_DIR_ENV_VAR, str(state_dir))
    monkeypatch.setenv(MINDS_CACHE_DIR_ENV_VAR, str(tmp_path / "c"))
    monkeypatch.setenv(MINDS_LOG_DIR_ENV_VAR, str(tmp_path / "l"))
    check_legacy_dotfolder_has_migrated()


def test_an_unactivated_shell_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``minds-admin`` leaves the root name unset, and then no tier is named to check."""
    _unmigrated_home(monkeypatch, tmp_path)
    monkeypatch.delenv(MINDS_ROOT_NAME_ENV_VAR)
    check_legacy_dotfolder_has_migrated()
