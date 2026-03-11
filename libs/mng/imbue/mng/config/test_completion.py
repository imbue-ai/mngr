"""Integration tests for the tab completion cache.

These tests run write_cli_completions_cache against the real CLI group to
verify that actual command options end up in the cache. This catches renames
(e.g. --base-branch -> --branch) that unit tests with hand-crafted data miss.
"""

import json
from pathlib import Path

from imbue.mng.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mng.config.completion_cache import CompletionCacheData
from imbue.mng.config.completion_writer import write_cli_completions_cache
from imbue.mng.config.data_types import MngContext
from imbue.mng.main import cli


def _read_cache(cache_dir: Path) -> CompletionCacheData:
    data = json.loads((cache_dir / COMPLETION_CACHE_FILENAME).read_text())
    return CompletionCacheData(**{k: v for k, v in data.items() if k in CompletionCacheData._fields})


def test_cache_contains_all_top_level_commands(completion_cache_dir: Path) -> None:
    """Every command registered on the real CLI group should appear in the cache."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for name in cli.commands:
        assert name in cache.commands, f"Command {name!r} missing from completion cache"


def test_cache_dynamic_choices_match_real_options(
    completion_cache_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """Dynamic choice option keys in the cache must reference options that actually exist."""
    write_cli_completions_cache(cli_group=cli, mng_ctx=temp_mng_ctx)
    cache = _read_cache(completion_cache_dir)

    for choice_key, values in cache.option_choices.items():
        parts = choice_key.split(".")
        # choice_key is "command.--option" or "command.subcommand.--option"
        option_name = parts[-1]
        assert option_name.startswith("--"), f"Unexpected choice key format: {choice_key}"

        # Walk the CLI tree to find the command
        cmd = cli
        for part in parts[:-1]:
            assert part in cmd.commands, f"Choice key {choice_key!r} references command {part!r} which does not exist"
            cmd = cmd.commands[part]

        # Verify the option exists on the command
        option_names = set()
        for param in cmd.params:
            if hasattr(param, "opts"):
                option_names.update(param.opts)
                option_names.update(param.secondary_opts)
        assert option_name in option_names, (
            f"Choice key {choice_key!r} references option {option_name!r} "
            f"which does not exist on the command. Available: {sorted(option_names)}"
        )


def test_cache_git_branch_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Git branch option keys must reference options that actually exist on the CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.git_branch_options:
        parts = key.split(".")
        option_name = parts[-1]
        cmd = cli
        for part in parts[:-1]:
            assert part in cmd.commands, f"git_branch_options key {key!r} references missing command {part!r}"
            cmd = cmd.commands[part]

        option_names = set()
        for param in cmd.params:
            if hasattr(param, "opts"):
                option_names.update(param.opts)
                option_names.update(param.secondary_opts)
        assert option_name in option_names, (
            f"git_branch_options key {key!r} references {option_name!r} "
            f"which does not exist. Available: {sorted(option_names)}"
        )


def test_cache_host_name_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Host name option keys must reference options that actually exist on the CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.host_name_options:
        parts = key.split(".")
        option_name = parts[-1]
        cmd = cli
        for part in parts[:-1]:
            assert part in cmd.commands, f"host_name_options key {key!r} references missing command {part!r}"
            cmd = cmd.commands[part]

        option_names = set()
        for param in cmd.params:
            if hasattr(param, "opts"):
                option_names.update(param.opts)
                option_names.update(param.secondary_opts)
        assert option_name in option_names, (
            f"host_name_options key {key!r} references {option_name!r} "
            f"which does not exist. Available: {sorted(option_names)}"
        )


def test_cache_dynamic_completions_populated(
    completion_cache_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """When mng_ctx is provided, dynamic completion values should be non-empty."""
    write_cli_completions_cache(cli_group=cli, mng_ctx=temp_mng_ctx)
    cache = _read_cache(completion_cache_dir)

    # Agent types should include at least the built-in registered types
    assert "create.--type" in cache.option_choices
    assert len(cache.option_choices["create.--type"]) > 0

    # Provider names always include "local"
    assert "create.--in" in cache.option_choices
    assert "local" in cache.option_choices["create.--in"]

    # Config keys are flattened from the config model
    assert len(cache.config_keys) > 0

    # Plugin names come from the plugin manager
    assert isinstance(cache.plugin_names, list)
