"""Unit tests for create_plugin_manager."""

import importlib.metadata
import os
from pathlib import Path

import pytest

from imbue.mngr.cli.plugin import plugin as plugin_group
from imbue.mngr.main import _PLUGIN_GROUP_INVOCATION_NAMES
from imbue.mngr.main import _PLUGIN_RECOVERY_SUBCOMMANDS
from imbue.mngr.main import _is_plugin_recovery_invocation
from imbue.mngr.main import cli
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.utils.env_utils import parse_bool_env


def test_create_plugin_manager_blocks_disabled_plugins(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """create_plugin_manager should block plugins disabled in config files."""
    # MNGR_LOAD_ALL_PLUGINS disables config-based blocking, so if it is set it would
    # silently mask this test. It must never be set during a normal test run, so treat
    # its presence as a leak and fail loudly -- some other test or imported module set
    # it process-wide (e.g. importing scripts/make_cli_docs, which sets it at import
    # time and is expected to pop it again). Surface the leak so it gets fixed at the
    # source rather than papered over here.
    assert not parse_bool_env(os.environ.get("MNGR_LOAD_ALL_PLUGINS", "")), (
        "MNGR_LOAD_ALL_PLUGINS is set in the test environment, which disables plugin "
        "blocking and would mask this test. It leaked into the process from another "
        "test or an imported module (e.g. an importer of scripts/make_cli_docs that "
        "failed to pop it). Find and contain the leak at its source."
    )
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )

    pm = create_plugin_manager(load_entry_points=True)

    assert pm.is_blocked("modal")


def test_create_plugin_manager_skips_blocking_when_load_all_plugins_set(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_plugin_manager should skip blocking when MNGR_LOAD_ALL_PLUGINS is truthy."""
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")

    pm = create_plugin_manager(load_entry_points=True)

    assert not pm.is_blocked("modal")


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["mngr", "plugin", "remove", "imbue-mngr-fake"], True),
        (["mngr", "plugin", "disable", "fake"], True),
        (["mngr", "plug", "remove", "imbue-mngr-fake"], True),
        (["mngr", "plug", "disable", "fake"], True),
        (["mngr", "plugin", "disable"], True),
        # Non-recovery plugin subcommands must still load plugins (fail loudly if broken).
        (["mngr", "plugin", "list"], False),
        (["mngr", "plugin", "add", "imbue-mngr-fake"], False),
        (["mngr", "plugin", "enable", "fake"], False),
        # Unrelated commands, bare group, and too-short argv.
        (["mngr", "create", "remove"], False),
        (["mngr", "remove"], False),
        (["mngr", "plugin"], False),
        (["mngr"], False),
        ([], False),
    ],
)
def test_is_plugin_recovery_invocation(argv: list[str], expected: bool) -> None:
    """Only `plugin remove` / `plugin disable` (and the `plug` alias) are recovery invocations."""
    assert _is_plugin_recovery_invocation(argv) is expected


def test_recovery_invocation_constants_match_click_tree() -> None:
    """The hardcoded recovery-detection sets must stay in sync with the real command tree.

    The detection in _is_plugin_recovery_invocation is hardcoded because it runs from
    sys.argv before Click parses arguments. This test fails if the `plugin` group's names
    (canonical + aliases) or its subcommands drift away from those hardcoded sets, so the
    fast-path cannot silently diverge from what Click actually dispatches.
    """
    reachable_names = frozenset(name for name, command in cli.commands.items() if command is plugin_group)
    assert reachable_names == _PLUGIN_GROUP_INVOCATION_NAMES, (
        f"The `plugin` group is reachable as {sorted(reachable_names)}, but recovery detection "
        f"matches {sorted(_PLUGIN_GROUP_INVOCATION_NAMES)}. Update _PLUGIN_GROUP_INVOCATION_NAMES."
    )

    subcommand_names = frozenset(plugin_group.commands.keys())
    assert _PLUGIN_RECOVERY_SUBCOMMANDS <= subcommand_names, (
        f"Recovery subcommands {sorted(_PLUGIN_RECOVERY_SUBCOMMANDS)} are not all real `mngr plugin` "
        f"subcommands {sorted(subcommand_names)}. Update _PLUGIN_RECOVERY_SUBCOMMANDS."
    )


def test_create_plugin_manager_skips_entry_points_in_recovery(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """With load_entry_points=False, no third-party setuptools entry point is imported.

    All provider/agent plugins come from setuptools entry points; skipping them is what lets
    `plugin remove` / `plugin disable` run even when one of those entry points fails to
    import. mngr's own built-ins (registered directly, not via entry points) still load.
    """
    (project_config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n")

    pm = create_plugin_manager(load_entry_points=False)

    entry_point_names = {ep.name for ep in importlib.metadata.entry_points(group="mngr")}
    # list_name_plugin() includes blocked names with a None plugin object, so filter to
    # names that were actually registered.
    registered_names = {name for name, plugin in pm.list_name_plugin() if plugin is not None}
    assert registered_names.isdisjoint(entry_point_names), (
        f"recovery mode registered entry-point plugins: {sorted(registered_names & entry_point_names)}"
    )
    # A built-in is still registered, so recovery is not a completely empty manager.
    assert pm.get_plugin("builtin_help_topics") is not None


def test_create_plugin_manager_blocks_disabled_plugins_even_in_recovery(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """Blocking of config-disabled plugins still runs when entry points are skipped.

    Blocking imports nothing, and it marks disabled names as blocked so the strict re-block
    in load_config does not trip on a name that is neither registered (nothing was loaded in
    recovery mode) nor blocked.
    """
    (project_config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n[plugins.modal]\nenabled = false\n"
    )

    pm = create_plugin_manager(load_entry_points=False)

    assert pm.is_blocked("modal")


def test_create_plugin_manager_broken_entry_point_only_crashes_full_load(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin whose import fails crashes a full load but not a recovery load.

    This is the core guarantee behind ``mngr plugin remove`` / ``mngr plugin disable``: those
    commands build the manager with load_entry_points=False (see get_or_create_plugin_manager),
    so a plugin whose import blows up (e.g. a provider whose dependency is missing or was
    renamed) cannot brick them, while every other command (load_entry_points=True) still fails
    loudly rather than running in a degraded state.
    """
    (project_config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n")

    # Register a fake "mngr" entry point whose module imports a nonexistent dependency, so
    # loading it raises ModuleNotFoundError -- exactly how a real plugin breaks when a
    # dependency is missing or was renamed. The sentinel name lets us assert the failure is
    # ours (via ModuleNotFoundError.name) without matching on the message text.
    missing_dep = "mngr_missing_dependency_for_broken_plugin_test"
    (tmp_path / "broken_ep_mod.py").write_text(f"import {missing_dep}\n")
    dist_info = tmp_path / "imbue_mngr_brokentest-0.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: imbue-mngr-brokentest\nVersion: 0.0.0\n")
    (dist_info / "entry_points.txt").write_text("[mngr]\nbrokentest = broken_ep_mod\n")
    # syspath_prepend is required: pluggy's load_setuptools_entrypoints discovers plugins by
    # scanning importlib.metadata.distributions() over sys.path, so the fake .dist-info must be
    # on sys.path (and its module importable) to be found. Registering a fake object with
    # pm.register(), as the other plugin tests do, does not work here: it never imports
    # anything, so it cannot reproduce an import failure. Undone on teardown.
    monkeypatch.syspath_prepend(str(tmp_path))

    # Recovery load (what plugin remove / disable use) survives: the broken entry point is
    # never imported, and the manager still has mngr's built-ins.
    recovery_pm = create_plugin_manager(load_entry_points=False)
    assert not recovery_pm.has_plugin("brokentest")
    assert recovery_pm.get_plugin("builtin_help_topics") is not None

    # Full load (what every other command uses) crashes on the broken import. Asserting on
    # ModuleNotFoundError.name confirms it failed on *our* plugin, not some unrelated error.
    with pytest.raises(ModuleNotFoundError) as excinfo:
        create_plugin_manager(load_entry_points=True)
    assert excinfo.value.name == missing_dep
