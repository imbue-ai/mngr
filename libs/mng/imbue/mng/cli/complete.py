"""Lightweight tab completion entrypoint -- no heavy third-party imports.

Reads COMP_WORDS and COMP_CWORD from the environment (same protocol click
uses), resolves command completions from a JSON cache file and agent name
completions from the discovery event stream, then prints results. This
avoids importing click, pydantic, pluggy, or any plugin code on every TAB
press.

Invoked as: python -m imbue.mng.cli.complete {zsh|bash}
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from typing import NamedTuple

from imbue.mng.cli.complete_names import resolve_names_from_discovery_stream
from imbue.mng.config.host_dir import read_default_host_dir

_COMMAND_COMPLETIONS_CACHE_FILENAME = ".command_completions.json"


class _CompletionContext(NamedTuple):
    """Parsed shell completion state derived from COMP_WORDS and the cache."""

    incomplete: str
    comp_cword: int
    prev_word: str | None
    command_key: str
    resolved_command: str | None
    is_group: bool
    commands: list[str]
    aliases: dict[str, str]
    subcommand_by_command: dict[str, list[str]]
    options_by_command: dict[str, list[str]]
    flag_options_by_command: dict[str, list[str]]
    cache: dict[str, Any]


def _get_completion_cache_dir() -> Path:
    """Return the directory used for completion cache files.

    Mirrors get_completion_cache_dir() in completion_writer.py.
    """
    env_dir = os.environ.get("MNG_COMPLETION_CACHE_DIR")
    if env_dir:
        return Path(env_dir)
    return read_default_host_dir()


def _read_cache() -> dict:
    """Read the command completions cache file. Returns empty dict on any error."""
    try:
        path = _get_completion_cache_dir() / _COMMAND_COMPLETIONS_CACHE_FILENAME
        if not path.is_file():
            return {}
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _read_host_names() -> list[str]:
    """Read host names from the discovery event stream."""
    try:
        _, host_names = resolve_names_from_discovery_stream()
        return host_names
    except (OSError, json.JSONDecodeError):
        return []


def _read_discovery_names() -> tuple[list[str], list[str]]:
    """Read both agent and host names from the discovery event stream in one pass."""
    try:
        return resolve_names_from_discovery_stream()
    except (OSError, json.JSONDecodeError):
        return [], []


def _read_git_branches() -> list[str]:
    """Read local and remote git branch names via ``git for-each-ref``."""
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/", "refs/remotes/"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line]
    except (OSError, subprocess.TimeoutExpired):
        return []


def _is_flag_option(word: str, flag_options: list[str]) -> bool:
    """Check if word is a known flag option.

    Handles exact matches (--force, -f) and combined short flags (-fb).
    For combined short flags, every character after the leading dash must
    map to a known single-character flag in flag_options.
    """
    if word in flag_options:
        return True
    if not word.startswith("-") or word.startswith("--") or len(word) < 3:
        return False
    return all(f"-{ch}" in flag_options for ch in word[1:])


def _parse_completion_context() -> _CompletionContext | None:
    """Parse COMP_WORDS, COMP_CWORD, and the cache into a structured context.

    Returns None if the environment is invalid (e.g. COMP_CWORD is not an int).
    """
    comp_words_raw = os.environ.get("COMP_WORDS", "")
    comp_cword_raw = os.environ.get("COMP_CWORD", "")

    try:
        comp_cword = int(comp_cword_raw)
    except ValueError:
        return None

    words = comp_words_raw.split()
    incomplete = words[comp_cword] if comp_cword < len(words) else ""

    cache = _read_cache()
    aliases: dict[str, str] = cache.get("aliases", {})
    subcommand_by_command: dict[str, list[str]] = cache.get("subcommand_by_command", {})

    # words[0] = "mng", words[1] = command, words[2] = subcommand (if group)
    resolved_command: str | None = None
    if len(words) > 1 and comp_cword > 1:
        raw_cmd = words[1]
        resolved_command = aliases.get(raw_cmd, raw_cmd)

    is_group = resolved_command is not None and resolved_command in subcommand_by_command
    resolved_subcommand: str | None = None
    if resolved_command is not None and is_group and len(words) > 2 and comp_cword > 2:
        resolved_subcommand = words[2]

    prev_word: str | None = None
    if comp_cword >= 1 and comp_cword - 1 < len(words):
        prev_word = words[comp_cword - 1]

    if resolved_subcommand is not None:
        command_key = f"{resolved_command}.{resolved_subcommand}"
    elif resolved_command is not None:
        command_key = resolved_command
    else:
        command_key = ""

    return _CompletionContext(
        incomplete=incomplete,
        comp_cword=comp_cword,
        prev_word=prev_word,
        command_key=command_key,
        resolved_command=resolved_command,
        is_group=is_group,
        commands=cache.get("commands", []),
        aliases=aliases,
        subcommand_by_command=subcommand_by_command,
        options_by_command=cache.get("options_by_command", {}),
        flag_options_by_command=cache.get("flag_options_by_command", {}),
        cache=cache,
    )


def _get_completions() -> list[str]:
    """Compute completion candidates from environment variables and the cache."""
    ctx = _parse_completion_context()
    if ctx is None:
        return []

    candidates: list[str]

    if ctx.comp_cword == 1:
        candidates = _filter_aliases(ctx.commands, ctx.aliases, ctx.incomplete)
    elif ctx.is_group and ctx.comp_cword == 2:
        assert ctx.resolved_command is not None
        candidates = ctx.subcommand_by_command.get(ctx.resolved_command, [])
    elif ctx.prev_word is not None and ctx.prev_word.startswith("-"):
        flag_options = ctx.flag_options_by_command.get(ctx.command_key, [])
        if _is_flag_option(ctx.prev_word, flag_options):
            if ctx.incomplete.startswith("--"):
                candidates = ctx.options_by_command.get(ctx.command_key, [])
            else:
                candidates = _get_positional_candidates(ctx.command_key, ctx.cache)
        elif ctx.incomplete.startswith("--"):
            candidates = ctx.options_by_command.get(ctx.command_key, [])
        else:
            choice_key = f"{ctx.command_key}.{ctx.prev_word}"
            candidates = _get_option_value_candidates(choice_key, ctx.cache)
    elif ctx.incomplete.startswith("--"):
        candidates = ctx.options_by_command.get(ctx.command_key, [])
    else:
        candidates = _get_positional_candidates(ctx.command_key, ctx.cache)

    return [c for c in candidates if c.startswith(ctx.incomplete)]


def _filter_aliases(
    commands: list[str],
    aliases: dict[str, str],
    incomplete: str,
) -> list[str]:
    """Filter command candidates, dropping aliases when their canonical name also matches.

    Mirrors the alias filtering logic from AliasAwareGroup.shell_complete.
    """
    matching = [c for c in commands if c.startswith(incomplete)]
    matching_set = set(matching)
    return [c for c in matching if c not in aliases or aliases[c] not in matching_set]


def _get_option_value_candidates(choice_key: str, cache: dict[str, Any]) -> list[str]:
    """Return completion candidates for a value-taking option.

    choice_key is the dotted key like "create.--host" or "list.--on-error".
    Checks predefined choices, git branches, host names, and plugin names.
    """
    option_choices: dict[str, list[str]] = cache.get("option_choices", {})
    if choice_key in option_choices:
        return option_choices[choice_key]

    git_branch_options: list[str] = cache.get("git_branch_options", [])
    if choice_key in git_branch_options:
        return _read_git_branches()

    host_name_options: list[str] = cache.get("host_name_options", [])
    if choice_key in host_name_options:
        return _read_host_names()

    plugin_name_options: list[str] = cache.get("plugin_name_options", [])
    if choice_key in plugin_name_options:
        return cache.get("plugin_names", [])

    return []


def _get_positional_candidates(command_key: str, cache: dict[str, Any]) -> list[str]:
    """Return positional argument candidates from all applicable completion sources.

    command_key is the dotted command key (e.g. "destroy", "snapshot.create", or "").
    Checks agent names, host names, plugin names, and config keys as appropriate.
    """
    if not command_key:
        return []

    candidates: list[str] = []

    needs_agents = command_key in cache.get("agent_name_arguments", [])
    needs_hosts = command_key in cache.get("host_name_arguments", [])
    if needs_agents or needs_hosts:
        agent_names, host_names = _read_discovery_names()
        if needs_agents:
            candidates.extend(agent_names)
        if needs_hosts:
            candidates.extend(host_names)
    if command_key in cache.get("plugin_name_arguments", []):
        candidates.extend(cache.get("plugin_names", []))
    if command_key in cache.get("config_key_arguments", []):
        candidates.extend(cache.get("config_keys", []))

    return candidates


def _generate_zsh_script() -> str:
    """Generate the zsh completion script with the current python path baked in."""
    python_path = sys.executable
    return f"""_mng_complete() {{
    local -a completions
    (( ! $+commands[mng] )) && return 1
    completions=(${{(@f)"$(COMP_WORDS="${{words[*]}}" COMP_CWORD=$((CURRENT-1)) {python_path} -m imbue.mng.cli.complete)"}})
    compadd -U -V unsorted -a completions
}}
compdef _mng_complete mng"""


def _generate_bash_script() -> str:
    """Generate the bash completion script with the current python path baked in."""
    python_path = sys.executable
    return f"""_mng_complete() {{
    local IFS=$'\\n'
    COMPREPLY=($(COMP_WORDS="${{COMP_WORDS[*]}}" COMP_CWORD="$COMP_CWORD" {python_path} -m imbue.mng.cli.complete))
}}
complete -o default -F _mng_complete mng"""


def main() -> None:
    """Entry point for lightweight tab completion.

    Usage:
        python -m imbue.mng.cli.complete
            Complete (reads COMP_WORDS/COMP_CWORD from the environment).
        python -m imbue.mng.cli.complete --script zsh
            Print the zsh completion script to stdout.
        python -m imbue.mng.cli.complete --script bash
            Print the bash completion script to stdout.
    """
    args = sys.argv[1:]

    if len(args) >= 2 and args[0] == "--script":
        shell = args[1]
        if shell == "zsh":
            sys.stdout.write(_generate_zsh_script() + "\n")
        else:
            sys.stdout.write(_generate_bash_script() + "\n")
        return

    completions = _get_completions()
    if completions:
        sys.stdout.write("\n".join(completions) + "\n")


if __name__ == "__main__":
    main()
