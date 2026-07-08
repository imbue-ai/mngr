"""Lightweight tab completion entrypoint -- no heavy third-party imports.

Reads COMP_WORDS and COMP_CWORD from the environment (same protocol click
uses), resolves command completions from a JSON cache file and agent name
completions from the discovery event stream, then prints results. This
avoids importing click, pydantic, pluggy, or any plugin code on every TAB
press.

Invoked as: python -m imbue.mngr.cli.complete {zsh|bash}
"""

import json
import os
import subprocess
import sys
from typing import NamedTuple

from imbue.mngr.cli.complete_names import resolve_names_from_discovery_stream
from imbue.mngr.cli.completion_install import generate_completion_shim
from imbue.mngr.cli.completion_install import maybe_warn_stale_completion
from imbue.mngr.cli.completion_install import write_managed_completion_scripts
from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import CompletionCacheData
from imbue.mngr.config.completion_cache import get_completion_cache_dir


class _CompletionContext(NamedTuple):
    """Parsed shell completion state derived from COMP_WORDS and the cache."""

    incomplete: str
    comp_cword: int
    prev_word: str | None
    command_key: str
    resolved_command: str | None
    is_group: bool
    cache: CompletionCacheData
    positional_count: int = 0
    first_positional_word: str | None = None
    words: tuple[str, ...] = ()


def _read_cache() -> CompletionCacheData:
    """Read the command completions cache file. Returns defaults on any error."""
    try:
        path = get_completion_cache_dir() / COMPLETION_CACHE_FILENAME
        if not path.is_file():
            return CompletionCacheData()
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return CompletionCacheData(**{k: v for k, v in data.items() if k in CompletionCacheData._fields})
    except (json.JSONDecodeError, OSError):
        pass
    return CompletionCacheData()


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


def _consume_value_option(words: list[str], option_index: int, end_index: int) -> int:
    """Return the index just past a value-taking option at ``option_index`` and its value.

    The option consumes the following word (its value). In zsh that value is a
    single word (e.g. ``KEY=VALUE``); bash treats ``=`` as a word break, so the
    value arrives split as ``KEY``, ``=``, ``VALUE``. Coalesce any such ``= X``
    continuations (and a trailing lone ``=``) so the whole value is consumed and
    its pieces are not later miscounted as positional arguments.
    """
    # Skip the option name, then the (first) value word.
    i = option_index + 1
    if i >= end_index:
        return i
    i += 1
    # In bash a `=`-containing value is split on `=`; stitch the `= X` pieces back
    # on (the `=` word break, then the value piece after it).
    while i < end_index and words[i] == "=":
        i += 1
        if i < end_index:
            i += 1
    return i


def _count_positional_words(
    words: list[str],
    start_index: int,
    end_index: int,
    flag_options: list[str],
    all_options: list[str],
) -> int:
    """Count the number of positional words in words[start_index:end_index].

    Walks the words, skipping option names and their values:
    - Flag options (consume 1 word)
    - Value-taking options (consume the option name + its value)
    Everything else is counted as a positional word.
    """
    all_options_set = set(all_options)
    count = 0
    i = start_index
    while i < end_index:
        word = words[i]
        if word.startswith("-"):
            if _is_flag_option(word, flag_options):
                # Flag option: consumes only itself
                i += 1
            elif word in all_options_set:
                # Known value-taking option: consumes itself and its value
                i = _consume_value_option(words, i, end_index)
            else:
                # Unknown option-like word: conservatively skip it alone.
                # We cannot tell whether it takes a value, but skipping just
                # the flag word avoids under-counting positional args (which
                # would cause us to offer completions past the limit).
                i += 1
        else:
            count += 1
            i += 1
    return count


def _find_first_positional_word(
    words: list[str],
    start_index: int,
    end_index: int,
    flag_options: list[str],
    all_options: list[str],
) -> str | None:
    """Find the first positional word in words[start_index:end_index].

    Uses the same option-skipping logic as _count_positional_words to
    correctly skip option names and their values.
    """
    all_options_set = set(all_options)
    i = start_index
    while i < end_index:
        word = words[i]
        if word.startswith("-"):
            if _is_flag_option(word, flag_options):
                i += 1
            elif word in all_options_set:
                i = _consume_value_option(words, i, end_index)
            else:
                i += 1
        else:
            return word
    return None


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

    # words[0] = "mngr", words[1] = command, words[2] = subcommand (if group)
    resolved_command: str | None = None
    if len(words) > 1 and comp_cword > 1:
        raw_cmd = words[1]
        resolved_command = cache.aliases.get(raw_cmd, raw_cmd)

    is_group = resolved_command is not None and resolved_command in cache.subcommand_by_command
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

    # Count positional words already typed (excluding the current incomplete word).
    # Positional args start after the command word (index 2 for simple commands,
    # index 3 for group subcommands).
    arg_start = 3 if resolved_subcommand is not None else 2
    flag_options = cache.flag_options_by_command.get(command_key, [])
    all_options = cache.options_by_command.get(command_key, [])
    positional_count = _count_positional_words(words, arg_start, comp_cword, flag_options, all_options)

    # Extract the first positional word (needed for context-dependent completions
    # like config value choices that depend on the key at position 0).
    first_positional_word = _find_first_positional_word(words, arg_start, comp_cword, flag_options, all_options)

    return _CompletionContext(
        incomplete=incomplete,
        comp_cword=comp_cword,
        prev_word=prev_word,
        command_key=command_key,
        resolved_command=resolved_command,
        is_group=is_group,
        cache=cache,
        positional_count=positional_count,
        first_positional_word=first_positional_word,
        words=tuple(words),
    )


def _get_positional_candidates_with_nargs_limit(ctx: _CompletionContext) -> list[str]:
    """Return positional candidates, respecting the positional nargs limit.

    Returns an empty list if the number of positional words already typed
    has reached the command's positional argument limit.
    """
    nargs_limit = ctx.cache.positional_nargs_by_command.get(ctx.command_key)
    if nargs_limit is not None and ctx.positional_count >= nargs_limit:
        return []
    return _get_positional_candidates(
        ctx.command_key,
        ctx.positional_count,
        ctx.cache,
        first_positional_word=ctx.first_positional_word,
        incomplete=ctx.incomplete,
    )


def _get_completions() -> list[str]:
    """Compute completion candidates from environment variables and the cache."""
    ctx = _parse_completion_context()
    if ctx is None:
        return []

    # The ``-S``/``--setting`` KEY=VALUE override is a global common option, so
    # it is handled ahead of the generic per-command routing below. The helper
    # returns its candidates already prefix-filtered (the KEY=VALUE token is
    # split differently by each shell), or None when this is not a setting
    # context so normal completion proceeds.
    setting_candidates = _get_setting_candidates(ctx)
    if setting_candidates is not None:
        return setting_candidates

    candidates: list[str]

    c = ctx.cache
    if ctx.comp_cword == 1:
        candidates = _filter_aliases(c.commands, c.aliases, ctx.incomplete)
    elif ctx.is_group and ctx.comp_cword == 2:
        assert ctx.resolved_command is not None
        candidates = c.subcommand_by_command.get(ctx.resolved_command, [])
    elif ctx.prev_word is not None and ctx.prev_word.startswith("-"):
        flag_options = c.flag_options_by_command.get(ctx.command_key, [])
        if _is_flag_option(ctx.prev_word, flag_options):
            if ctx.incomplete.startswith("--"):
                candidates = c.options_by_command.get(ctx.command_key, [])
            else:
                candidates = _get_positional_candidates_with_nargs_limit(ctx)
        elif ctx.incomplete.startswith("--"):
            candidates = c.options_by_command.get(ctx.command_key, [])
        else:
            choice_key = f"{ctx.command_key}.{ctx.prev_word}"
            candidates = _get_option_value_candidates(choice_key, c)
    elif ctx.incomplete.startswith("--"):
        candidates = c.options_by_command.get(ctx.command_key, [])
    else:
        candidates = _get_positional_candidates_with_nargs_limit(ctx)

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


def _get_option_value_candidates(choice_key: str, cache: CompletionCacheData) -> list[str]:
    """Return completion candidates for a value-taking option.

    choice_key is the dotted key like "create.--host" or "list.--on-error".
    Checks predefined choices, git branches, host names, and plugin names.
    """
    if choice_key in cache.option_choices:
        return cache.option_choices[choice_key]
    if choice_key in cache.git_branch_options:
        return _read_git_branches()
    if choice_key in cache.host_name_options:
        return _read_host_names()
    if choice_key in cache.plugin_name_options:
        return cache.plugin_names
    return []


def _segment_keys(keys: list[str], incomplete: str) -> tuple[list[str], list[str]]:
    """Collapse dotted keys to the next ``.``-delimited segment relative to ``incomplete``.

    Returns ``(branches, leaves)`` for the keys that start with ``incomplete``:

    - ``branches`` are the distinct next-level prefixes that still have deeper
      keys below them, each ending in ``.`` (e.g. ``agent_types.``). Completing
      one drills down a level rather than dumping every descendant at once.
    - ``leaves`` are the full keys whose next segment is terminal (no further
      ``.`` below the current level).

    This gives a hierarchical, segment-at-a-time view (top-level keys first, then
    their sub-keys) instead of listing every fully-qualified key up front.
    """
    dot = incomplete.rfind(".")
    # The already-settled ``a.b.`` portion of what's typed ("" when there's no dot yet).
    base = incomplete[: dot + 1]
    branches: list[str] = []
    seen_branches: set[str] = set()
    leaves: list[str] = []
    for key in keys:
        if not key.startswith(incomplete):
            continue
        tail = key[len(base) :]
        next_dot = tail.find(".")
        if next_dot == -1:
            leaves.append(key)
        else:
            branch = base + tail[: next_dot + 1]
            if branch not in seen_branches:
                seen_branches.add(branch)
                branches.append(branch)
    return branches, leaves


def _setting_key_candidates(key_prefix: str, cache: CompletionCacheData) -> list[str]:
    """Key-phase candidates for ``-S``/``--setting`` (completing the KEY of KEY=VALUE).

    Dotted keys are collapsed to the next ``.`` segment (see ``_segment_keys``) so
    the user drills down one level at a time instead of seeing every descendant.
    Branch segments end in ``.`` (the shell suppresses the trailing space so the
    next segment can be typed). Leaf keys with a constrained value set are emitted
    as ``KEY=`` (also a no-trailing-space boundary): the values are deferred to the
    next TAB, mirroring the ``.`` drill-down rather than dumping every value as
    soon as the key prefix matches. Free-form leaf keys are emitted bare.
    """
    branches, leaves = _segment_keys(cache.config_keys, key_prefix)
    candidates: list[str] = list(branches)
    for key in leaves:
        if cache.config_value_choices.get(key):
            candidates.append(f"{key}=")
        else:
            candidates.append(key)
    return candidates


def _setting_value_candidates(
    key: str,
    value_prefix: str,
    cache: CompletionCacheData,
    prefix_with_key: bool,
) -> list[str]:
    """Value-phase candidates for ``-S``/``--setting`` (completing the VALUE of KEY=VALUE).

    With ``prefix_with_key`` (zsh, where ``KEY=VALUE`` is one word), each value is
    returned as ``KEY=VALUE`` so the whole word is replaced. Without it (bash,
    where ``=`` is a word break and only the trailing value word is replaced),
    the bare values are returned.
    """
    matching = [value for value in cache.config_value_choices.get(key, []) if value.startswith(value_prefix)]
    if prefix_with_key:
        return [f"{key}={value}" for value in matching]
    return matching


def _get_setting_candidates(ctx: _CompletionContext) -> list[str] | None:
    """Completion candidates for the ``-S``/``--setting`` KEY=VALUE override option.

    Returns the final (already prefix-filtered) candidate list when the cursor is
    completing a setting value, or None when this is not a setting context (so the
    caller falls back to normal completion).

    The ``KEY=VALUE`` token is split differently by each shell, so three shapes
    are recognised:

    - The option is the previous word (``-S <cursor>`` or ``-S KEY=<cursor>``).
      This is the key phase in both shells and also the value phase in zsh, which
      keeps ``KEY=VALUE`` as a single word.
    - bash treats ``=`` as a word break, so ``KEY=VALPREFIX`` arrives as the
      separate words ``KEY``, ``=``, ``VALPREFIX``; the standalone ``=`` is the
      previous word.
    - bash with the cursor right after ``=`` (``-S KEY=<cursor>``), where the
      incomplete word is the standalone ``=`` itself.
    """
    setting_options = set(ctx.cache.setting_option_names)
    if not setting_options:
        return None

    incomplete = ctx.incomplete
    words = ctx.words

    # Key phase (both shells), or value phase in zsh: the option is the prev word.
    if ctx.prev_word in setting_options:
        if "=" in incomplete:
            key, _, value_prefix = incomplete.partition("=")
            return _setting_value_candidates(key, value_prefix, ctx.cache, prefix_with_key=True)
        return _setting_key_candidates(incomplete, ctx.cache)

    # bash value phase with a typed prefix: ``-S KEY = VALPREFIX``.
    if ctx.prev_word == "=" and ctx.comp_cword >= 3 and words[ctx.comp_cword - 3] in setting_options:
        key = words[ctx.comp_cword - 2]
        return _setting_value_candidates(key, incomplete, ctx.cache, prefix_with_key=False)

    # bash value phase with the cursor right after ``=``: ``-S KEY =``.
    if incomplete == "=" and ctx.comp_cword >= 2 and words[ctx.comp_cword - 2] in setting_options:
        assert ctx.prev_word is not None
        return _setting_value_candidates(ctx.prev_word, "", ctx.cache, prefix_with_key=False)

    return None


def _resolve_sources(
    sources: list[str],
    cache: CompletionCacheData,
    first_positional_word: str | None = None,
    incomplete: str = "",
) -> list[str]:
    """Resolve completion source identifiers to actual candidate values.

    Source identifiers: "agent_names", "host_names", "plugin_names",
    "catalog_packages", "installed_packages", "help_targets", "config_keys",
    "config_value_for_key".

    The ``config_keys`` source is collapsed to the next ``.`` segment relative to
    ``incomplete`` (see ``_segment_keys``), so ``mngr config get/set/unset`` drills
    into a dotted key one level at a time rather than listing every descendant.
    """
    candidates: list[str] = []
    needs_agents = "agent_names" in sources
    needs_hosts = "host_names" in sources
    if needs_agents or needs_hosts:
        agent_names, host_names = _read_discovery_names()
        if needs_agents:
            candidates.extend(agent_names)
        if needs_hosts:
            candidates.extend(host_names)
    if "plugin_names" in sources:
        candidates.extend(cache.plugin_names)
    if "catalog_packages" in sources:
        candidates.extend(cache.catalog_package_names)
    if "installed_packages" in sources:
        candidates.extend(cache.installed_plugin_package_names)
    if "help_targets" in sources:
        candidates.extend(cache.help_targets)
    if "config_keys" in sources:
        branches, leaves = _segment_keys(cache.config_keys, incomplete)
        candidates.extend(branches)
        candidates.extend(leaves)
    if "config_value_for_key" in sources and first_positional_word:
        candidates.extend(cache.config_value_choices.get(first_positional_word, []))
    return candidates


def _get_positional_candidates(
    command_key: str,
    positional_count: int,
    cache: CompletionCacheData,
    first_positional_word: str | None = None,
    incomplete: str = "",
) -> list[str]:
    """Return positional argument candidates for a specific position.

    command_key is the dotted command key (e.g. "destroy", "snapshot.create", or "").
    positional_count is the number of positional words already typed.
    Looks up per-position sources from cache.positional_completions and resolves them.
    For variadic commands (nargs=None), the last entry repeats.
    """
    if not command_key:
        return []
    entries = cache.positional_completions.get(command_key)
    if not entries:
        return []
    idx = min(positional_count, len(entries) - 1)
    sources = entries[idx]
    if not sources:
        return []
    return _resolve_sources(sources, cache, first_positional_word=first_positional_word, incomplete=incomplete)


def main() -> None:
    """Entry point for lightweight tab completion.

    Usage:
        python -m imbue.mngr.cli.complete
            Complete (reads COMP_WORDS/COMP_CWORD from the environment).
        python -m imbue.mngr.cli.complete --script zsh
            Write the managed completion files and print the zsh rc shim to stdout.
        python -m imbue.mngr.cli.complete --script bash
            Write the managed completion files and print the bash rc shim to stdout.
    """
    args = sys.argv[1:]

    if len(args) >= 2 and args[0] == "--script":
        shell = args[1]
        # Materialise the managed files so the shim has something to source, then
        # emit the shim (the rc content).
        write_managed_completion_scripts()
        sys.stdout.write(generate_completion_shim(shell) + "\n")
        return

    maybe_warn_stale_completion()
    completions = _get_completions()
    if completions:
        sys.stdout.write("\n".join(completions) + "\n")


if __name__ == "__main__":
    main()
