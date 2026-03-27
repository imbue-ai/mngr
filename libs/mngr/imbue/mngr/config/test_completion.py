"""Integration tests for the tab completion cache.

These tests run write_cli_completions_cache against the real CLI group to
verify that hand-maintained completion constants reference options that
actually exist. This catches renames (e.g. --base-branch -> --branch)
that unit tests with hand-crafted data miss.
"""

import json
from pathlib import Path

import click

from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import CompletionCacheData
from imbue.mngr.config.completion_writer import write_cli_completions_cache
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.main import cli


def _read_cache(cache_dir: Path) -> CompletionCacheData:
    data = json.loads((cache_dir / COMPLETION_CACHE_FILENAME).read_text())
    return CompletionCacheData(**{k: v for k, v in data.items() if k in CompletionCacheData._fields})


def _assert_option_exists_on_cli(dotted_key: str, label: str) -> None:
    """Assert that a dotted key like "create.--host" references a real CLI option."""
    parts = dotted_key.split(".")
    option_name = parts[-1]
    assert option_name.startswith("--"), f"Unexpected key format in {label}: {dotted_key}"

    cmd = cli
    for part in parts[:-1]:
        assert isinstance(cmd, click.Group) and part in cmd.commands, (
            f"{label} key {dotted_key!r} references command {part!r} which does not exist"
        )
        cmd = cmd.commands[part]

    option_names = set()
    for param in cmd.params:
        if hasattr(param, "opts"):
            option_names.update(param.opts)
            option_names.update(param.secondary_opts)
    assert option_name in option_names, (
        f"{label} key {dotted_key!r} references {option_name!r} "
        f"which does not exist. Available: {sorted(option_names)}"
    )


def test_option_choices_reference_real_options(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Every option_choices key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(
        cli_group=cli, mngr_ctx=temp_mngr_ctx, registered_agent_types=list_registered_agent_types()
    )
    cache = _read_cache(completion_cache_dir)

    for choice_key in cache.option_choices:
        _assert_option_exists_on_cli(choice_key, "option_choices")


def test_git_branch_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Every git_branch_options key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.git_branch_options:
        _assert_option_exists_on_cli(key, "git_branch_options")


def test_host_name_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Every host_name_options key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.host_name_options:
        _assert_option_exists_on_cli(key, "host_name_options")


def test_plugin_name_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Every plugin_name_options key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.plugin_name_options:
        _assert_option_exists_on_cli(key, "plugin_name_options")


def _collect_all_options_from_cli() -> dict[str, set[str]]:
    """Walk the real CLI tree and collect all --long options keyed by dotted command path.

    Returns a dict mapping command key (e.g. "create", "config.set") to the set
    of --long option names on that command.
    """
    result: dict[str, set[str]] = {}
    assert isinstance(cli, click.Group)
    for name, cmd in cli.commands.items():
        if isinstance(cmd, click.Group) and cmd.commands:
            for sub_name, sub_cmd in cmd.commands.items():
                key = f"{cmd.name or name}.{sub_name}"
                opts: set[str] = set()
                for param in sub_cmd.params:
                    if isinstance(param, click.Option):
                        for opt in param.opts + param.secondary_opts:
                            if opt.startswith("--"):
                                opts.add(opt)
                if opts:
                    result[key] = opts
            # Also collect group-level options
            group_opts: set[str] = set()
            for param in cmd.params:
                if isinstance(param, click.Option):
                    for opt in param.opts + param.secondary_opts:
                        if opt.startswith("--"):
                            group_opts.add(opt)
            if group_opts:
                result[cmd.name or name] = group_opts
        else:
            key = cmd.name or name
            opts = set()
            for param in cmd.params:
                if isinstance(param, click.Option):
                    for opt in param.opts + param.secondary_opts:
                        if opt.startswith("--"):
                            opts.add(opt)
            if opts:
                result[key] = opts
    return result


def test_every_option_is_classified(completion_cache_dir: Path) -> None:
    """Every CLI --long option must appear in options_by_command in the cache.

    This catches options added to commands without updating the cache writer,
    and renames (e.g. --agent-type -> --type) that would go undetected.
    """
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    cli_options = _collect_all_options_from_cli()
    missing: list[str] = []

    for command_key, option_names in cli_options.items():
        cached_options = set(cache.options_by_command.get(command_key, []))
        for opt in sorted(option_names):
            if opt not in cached_options:
                missing.append(f"{command_key}.{opt}")

    assert not missing, "The following CLI options are not in options_by_command in the cache:\n" + "\n".join(
        f"  {m}" for m in missing
    )
