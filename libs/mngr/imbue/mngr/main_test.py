"""Unit tests for create_plugin_manager."""

from pathlib import Path

import pytest

from imbue.mngr.cli.help_formatter import get_help_metadata
from imbue.mngr.main import _resolve_builtin
from imbue.mngr.main import create_plugin_manager
from imbue.mngr.utils.builtin_command_specs import BUILTIN_COMMAND_SPECS


def test_create_plugin_manager_blocks_disabled_plugins(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
) -> None:
    """create_plugin_manager should block plugins disabled in config files."""
    (project_config_dir / "settings.toml").write_text("[plugins.modal]\nenabled = false\n")

    pm = create_plugin_manager()

    assert pm.is_blocked("modal")


def test_create_plugin_manager_skips_blocking_when_load_all_plugins_set(
    project_config_dir: Path,
    temp_git_repo_cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_plugin_manager should skip blocking when MNGR_LOAD_ALL_PLUGINS is truthy."""
    (project_config_dir / "settings.toml").write_text("[plugins.modal]\nenabled = false\n")
    monkeypatch.setenv("MNGR_LOAD_ALL_PLUGINS", "1")

    pm = create_plugin_manager()

    assert not pm.is_blocked("modal")


def test_builtin_specs_match_command_help_metadata() -> None:
    """Each ``BuiltinCommandSpec`` must mirror the command's ``CommandHelpMetadata``.

    The lazy-load registry duplicates ``one_line_description`` and ``aliases``
    so the root ``mngr --help`` can render rows without importing the command
    module. This test forces every builtin to load and asserts the duplicated
    text and aliases stay in sync, so updates to the metadata don't silently
    drift from the registry text.
    """
    drift: list[str] = []
    for spec in BUILTIN_COMMAND_SPECS:
        cmd = _resolve_builtin(spec.name)
        assert cmd is not None, f"{spec.name}: failed to resolve lazy builtin"
        canonical = cmd.name or spec.name
        metadata = get_help_metadata(canonical)
        if metadata is None:
            # Hidden / undocumented commands (e.g., ``dependencies``) need not
            # have CommandHelpMetadata registered.
            continue
        if metadata.one_line_description != spec.short_help:
            drift.append(
                f"{spec.name}: short_help={spec.short_help!r} "
                f"!= metadata.one_line_description={metadata.one_line_description!r}"
            )
        if metadata.aliases != spec.aliases:
            drift.append(f"{spec.name}: aliases={spec.aliases!r} != metadata.aliases={metadata.aliases!r}")
    assert drift == [], "Lazy-builtin registry has drifted from CommandHelpMetadata:\n  " + "\n  ".join(drift)
