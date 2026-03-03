# Critique: `mng create` CLI Interface

This document analyzes the `mng create` command's interface for inconsistency, complexity, and potential user confusion, based on the implementation in `cli/create.py` and the examples in `tutorials/mega_tutorial.sh`.

## 1. Naming Inconsistencies: `--source` vs `--from` vs `--source-*`

**Severity: HIGH**

The source specification is fragmented across multiple overlapping options with inconsistent naming:

- `--from` / `--source` (unified string: `AGENT | AGENT.HOST | AGENT.HOST:PATH | HOST:PATH`)
- `--source-agent` / `--from-agent`
- `--source-host`
- `--source-path`

The tutorial uses `--from other-agent` in one place but `--source-path /path/to/some/other/project` in another. The `--from` alias suggests a simpler mental model ("clone from this"), while `--source` suggests a more general concept. Having both, plus the decomposed `--source-agent`, `--source-host`, `--source-path` variants, means users have to decide between 4+ different ways to express the same thing.

**The core problem:** The unified `--source` format (`AGENT.HOST:PATH`) is powerful but its parsing rules are non-obvious (dots separate agent from host, colons separate the prefix from the path). Meanwhile, the decomposed options (`--source-agent`, `--source-host`, `--source-path`) are clear but verbose. Having both creates confusion about which to use and how they interact when combined.

**Suggestion:** Pick one pattern and commit to it. Either the unified string is the primary interface (and the decomposed options are hidden/advanced), or the decomposed options are primary (and the unified string is a shorthand).

## 2. `--host` vs `--in` vs `--new-host` vs `--host-name` vs `--target-host`

**Severity: HIGH**

The host targeting surface has a similar fragmentation problem:

- `--in` / `--new-host`: Create a *new* host on a provider (e.g., `--in modal`)
- `--host` / `--target-host`: Use an *existing* host by name or ID
- `--host-name`: Set the name for a *new* host
- `--target`: Target `[HOST][:PATH]` -- yet another way to specify a host

The tutorial uses `--in modal` for new hosts and `--host my-dev-box` for existing ones, which is reasonably clear. But `--host-name` only applies when `--in` is used (it names the *new* host), and `--target-host` is an alias for `--host`. The distinction between `--host` (existing host by name) and `--host-name` (name *for* the new host) is confusing since they sound nearly identical but do completely different things.

**The asymmetry is also confusing:** `--new-host` is the long form of `--in`, but there is no `--new-host-name` -- instead it's `--host-name`. So `--host` means "existing host" and `--host-name` means "name for the new host". A user reading `--host foo --host-name bar` would be forgiven for thinking those are related.

**Suggestion:** Make the naming consistently signal "new" vs "existing". For example, `--new-host-name` instead of `--host-name`, or unify around `--host` with sub-syntax.

## 3. `--build-arg` vs `--build-args` vs `-b` (and `--start-arg` vs `--start-args` vs `-s`)

**Severity: MEDIUM**

There are two separate patterns for passing build arguments:

- `-b` / `--build-arg`: repeatable, one key=value per flag (`-b cpu=4 -b memory=16`)
- `--build-args`: a single space-separated string (`--build-args "cpu=4 memory=16"`)

And the same duplication exists for start arguments (`-s`/`--start-arg` vs `--start-args`).

The tutorial demonstrates both in different places:
```bash
mng create my-task --in modal --build-arg cpu=4 --build-arg memory=16
mng create my-task --in modal --build-args "file=./Dockerfile.agent context-dir=./agent-context"
```

This is confusing because it's not obvious why both exist or when to use which. Furthermore, `-b` also has `--build` as a long-form alias, which reads oddly since `--build gpu=h100` doesn't clearly communicate "pass a build argument."

**Suggestion:** Eliminate `--build-args` (plural) entirely. The repeatable `-b`/`--build-arg` pattern already handles multiple values cleanly and is the standard CLI convention (cf. `docker build --build-arg`). The plural form just adds a second way to do the same thing with different escaping rules.

## 4. `--copy` vs `--clone` vs `--worktree` vs `--in-place` vs `--copy-work-dir`

**Severity: MEDIUM-HIGH**

The work directory isolation model is expressed through 4 mutually exclusive flags (`--in-place`, `--copy`, `--clone`, `--worktree`), plus a separate timing flag (`--copy-work-dir` / `--no-copy-work-dir`) that controls *when* the copy happens. The defaults change based on context:

- Local + git repo -> worktree
- Local + no git -> copy
- Remote -> copy
- `--in-place` -> no isolation

The `--copy-work-dir` flag is particularly confusing because it sounds like it selects the "copy" mode, but it actually controls whether the work directory is copied *immediately* (before the agent starts) vs lazily. Its name is misleading when combined with `--worktree` -- you might write `--worktree --copy-work-dir` which reads like a contradiction.

**Suggestion:** Rename `--copy-work-dir` to something that better communicates timing, like `--eager-copy` / `--lazy-copy` or `--copy-before-start` / `--copy-after-start`.

## 5. Agent Environment vs Host Environment

**Severity: MEDIUM**

There are two parallel sets of environment variable options:

Agent: `--env`, `--env-file`, `--pass-env` (aliases: `--agent-env`, `--agent-env-file`, `--pass-agent-env`)
Host: `--host-env`, `--host-env-file`, `--pass-host-env`

The short forms (`--env`, `--env-file`, `--pass-env`) apply to the *agent*, not the host. This is reasonable since the agent is the primary thing being created, but the tutorial explicitly warns:

> "it is *strongly encouraged* to use either --env-file or --pass-env, especially for any sensitive environment variables"

The existence of both agent-level and host-level env vars is inherently confusing to new users who don't yet understand the agent/host distinction. The tutorial only partially explains when you'd use host env vars vs agent env vars.

**Suggestion:** This is somewhat inherent to the agent/host model, but the documentation could be much clearer. Consider whether host env vars should be part of `--build-arg` instead (since they're about host construction).

## 6. `--message` vs `--message-file` vs `--edit-message` vs `--resume-message` vs `--resume-message-file`

**Severity: MEDIUM**

Five options for message handling, with complex interaction rules:

- `--message` and `--message-file` are mutually exclusive
- `--edit-message` opens an editor, optionally pre-filled with `--message` or `--message-file` content
- `--edit-message` is incompatible with `--no-connect --no-await-ready` (background creation)
- `--resume-message` and `--resume-message-file` are separate from the initial message and only apply when an agent is restarted

The resume message is particularly confusing because it's on the `create` command but only applies in a specific scenario (agent already existed and was stopped, now being resumed via `--reuse`).

**Suggestion:** Consider whether `--resume-message` belongs on `create` at all, or whether it should be on `start` instead. Also consider accepting `-` for stdin and eliminating the separate `--message-file` flags (use `--message "$(cat file)"` or `--message -`).

## 7. Boolean Flag Defaults That Depend on Other Flags

**Severity: MEDIUM**

Several flags have defaults that silently change based on other flags:

- `--await-ready` defaults to false, but `--await-agent-stopped` implies `--await-ready`
- `--connect` defaults to true, but `--await-agent-stopped` implies `--no-connect`
- `--copy-work-dir` defaults to true if `--no-connect`, false if `--connect`
- `--include-unclean` defaults based on `--ensure-clean`
- `--rsync` defaults based on presence of `--rsync-args` or absence of git
- `--new-branch` defaults to yes, but `--in-place` implies `--no-new-branch`

While each individual default makes sense, the compound effect is a complex web of interdependencies. A user passing `--no-connect` silently changes the behavior of `--copy-work-dir` and `--await-ready`. This makes the command harder to reason about.

**Suggestion:** Consider logging a message when a default is silently adjusted (e.g., "Note: --copy-work-dir defaulting to true because --no-connect was specified"). This would make the implicit relationships visible.

## 8. The `--target` Option Overlaps with `--host` and `--target-path`

**Severity: LOW-MEDIUM**

`--target` accepts `[HOST][:PATH]` format, which overlaps with:
- `--host` / `--target-host` (for the host part)
- `--target-path` (for the path part)

This is a miniature version of the `--source` fragmentation problem. The tutorial doesn't even use `--target`, relying on `--target-path` and `--host` separately, which raises the question of whether `--target` provides enough value to justify the additional surface area.

**Suggestion:** Consider removing `--target` and keeping only the decomposed `--host` + `--target-path`.

## 9. `-c` / `--add-command` Has a Surprising Parsing Rule

**Severity: LOW**

The help text states: "Note: ALL_UPPERCASE names (e.g., FOO='bar') are treated as env var assignments, not window names." This is a surprising heuristic. A user who names their window `DEBUG="tail -f debug.log"` would get unexpected behavior. The convention of using uppercase to distinguish env var assignments from window names is not something a user would discover without reading the fine print.

**Suggestion:** Use a different syntax to distinguish env vars from commands, or remove the ability to set env vars via `--add-command` entirely (use `--env` instead).

## 10. Positional Arguments Interact Awkwardly with `--`

**Severity: LOW**

The command accepts `POSITIONAL_NAME`, `POSITIONAL_AGENT_TYPE`, and then `AGENT_ARGS` (after `--`). The code has an explicit workaround (`_was_value_after_double_dash`) to detect when click incorrectly assigns a value that was meant to be in `agent_args` to `positional_agent_type`. This is a sign that the positional argument design fights against the `--` separator convention.

For example, `mng create -- --model opus` should pass `--model opus` to the agent, but click may try to consume the first value after `--` as `positional_agent_type`.

**Suggestion:** This is partially a limitation of click, but it could be mitigated by making agent type exclusively a named option (`--agent-type`) and removing the positional `AGENT_TYPE` argument.

## Summary of Recommendations (by impact)

1. **Unify source specification** -- reduce `--from`/`--source`/`--source-agent`/`--source-host`/`--source-path` to fewer options with a clear primary pattern
2. **Fix host naming confusion** -- rename `--host-name` to `--new-host-name` or similar to distinguish from `--host`
3. **Eliminate `--build-args` plural form** -- keep only the repeatable `-b`/`--build-arg`
4. **Rename `--copy-work-dir`** -- the current name is misleading about what it controls
5. **Consider moving `--resume-message` to `start`** -- it doesn't belong on `create`
6. **Log when defaults change implicitly** -- make the interdependency web visible
7. **Drop `--target` unified option** -- keep only `--host` + `--target-path`
8. **Fix `--add-command` env var heuristic** -- use a less surprising mechanism
