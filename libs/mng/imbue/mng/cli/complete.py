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
import sys
from pathlib import Path

from imbue.mng.cli.complete_names import resolve_names_from_discovery_stream
from imbue.mng.config.host_dir import read_default_host_dir

_COMMAND_COMPLETIONS_CACHE_FILENAME = ".command_completions.json"


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


def _read_agent_names() -> list[str]:
    """Read agent names from the discovery event stream."""
    try:
        agent_names, _ = resolve_names_from_discovery_stream()
        return agent_names
    except (OSError, json.JSONDecodeError):
        return []


def _get_completions() -> list[str]:
    """Compute completion candidates from environment variables and the cache."""
    comp_words_raw = os.environ.get("COMP_WORDS", "")
    comp_cword_raw = os.environ.get("COMP_CWORD", "")

    try:
        comp_cword = int(comp_cword_raw)
    except ValueError:
        return []

    words = comp_words_raw.split()

    # Determine the incomplete word being completed
    if comp_cword < len(words):
        incomplete = words[comp_cword]
    else:
        incomplete = ""

    cache = _read_cache()
    commands: list[str] = cache.get("commands", [])
    aliases: dict[str, str] = cache.get("aliases", {})
    subcommand_by_command: dict[str, list[str]] = cache.get("subcommand_by_command", {})
    options_by_command: dict[str, list[str]] = cache.get("options_by_command", {})
    flag_options_by_command: dict[str, list[str]] = cache.get("flag_options_by_command", {})
    option_choices: dict[str, list[str]] = cache.get("option_choices", {})
    agent_name_arguments: list[str] = cache.get("agent_name_arguments", [])

    # Resolve the command and subcommand from the words already typed
    resolved_command: str | None = None
    resolved_subcommand: str | None = None

    # words[0] = "mng", words[1] = command, words[2] = subcommand (if group)
    # When comp_cword == 1, we are completing the command name itself and
    # resolved_command stays None. When comp_cword > 1, word[1] is fully typed.
    if len(words) > 1 and comp_cword > 1:
        raw_cmd = words[1]
        resolved_command = aliases.get(raw_cmd, raw_cmd)

    is_group = resolved_command is not None and resolved_command in subcommand_by_command

    if resolved_command is not None and is_group and len(words) > 2 and comp_cword > 2:
        resolved_subcommand = words[2]

    # Determine the previous word (for option value completion)
    prev_word: str | None = None
    if comp_cword >= 1 and comp_cword - 1 < len(words):
        prev_word = words[comp_cword - 1]

    # Determine the command key for option lookups
    if resolved_subcommand is not None:
        option_key = f"{resolved_command}.{resolved_subcommand}"
    elif resolved_command is not None:
        option_key = resolved_command
    else:
        option_key = ""

    candidates: list[str]

    if comp_cword == 1:
        # Completing the command name (position 1)
        candidates = _filter_aliases(commands, aliases, incomplete)
    elif is_group and comp_cword == 2:
        # Completing a subcommand of a group
        assert resolved_command is not None
        candidates = subcommand_by_command.get(resolved_command, [])
    elif prev_word is not None and prev_word.startswith("-"):
        choice_key = f"{option_key}.{prev_word}"
        flag_options = flag_options_by_command.get(option_key, [])
        if choice_key in option_choices:
            # Option with predefined choices (e.g. --on-error abort|continue)
            candidates = option_choices[choice_key]
        elif prev_word in flag_options:
            # Previous word is a flag -- next position is positional
            if incomplete.startswith("--"):
                candidates = options_by_command.get(option_key, [])
            else:
                candidates = _get_positional_candidates(option_key, agent_name_arguments)
        elif incomplete.startswith("--"):
            # Previous word is value-taking, but user started typing an option
            candidates = options_by_command.get(option_key, [])
        else:
            # Previous word is value-taking, current word is its value -- no completions
            candidates = []
    elif incomplete.startswith("--"):
        candidates = options_by_command.get(option_key, [])
    else:
        candidates = _get_positional_candidates(option_key, agent_name_arguments)

    return [c for c in candidates if c.startswith(incomplete)]


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


def _get_positional_candidates(
    command_key: str,
    agent_name_arguments: list[str],
) -> list[str]:
    """Return positional argument candidates (agent names) if applicable.

    command_key is the dotted command key (e.g. "destroy", "snapshot.create", or "").
    """
    if command_key and command_key in agent_name_arguments:
        return _read_agent_names()
    else:
        return []


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
