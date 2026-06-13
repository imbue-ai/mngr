# Unabridged Changelog - mngr_claude_subagent_proxy

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_claude_subagent_proxy/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-12

Internal: routed `host_dir / "agents"` path constructions through the shared `get_agents_root_dir` / `get_agent_state_dir_path` helpers (now in `imbue.mngr.hosts.common`). No behavior change.

The plugin's provisioning artifacts now live under a `mngr-proxy/` subdirectory and are guarded against dirtying a tracked worktree:

- The PROXY-mode agent definition moved from `.claude/agents/mngr-proxy.md` to `.claude/agents/mngr-proxy/proxy.md`, and the DENY-mode skill moved from `.claude/skills/mngr-subagents/SKILL.md` to `.claude/skills/mngr-proxy/SKILL.md` (the DENY skill is correspondingly renamed from `mngr-subagents` to `mngr-proxy`). A single `.claude/agents/mngr-proxy/` or `.claude/skills/mngr-proxy/` line in `.gitignore` now covers each artifact. Discovery is unaffected: Claude Code identifies the subagent by its frontmatter `name:` field.

- At provisioning the plugin now refuses to write either artifact into a git-tracked worktree where the path is not gitignored, raising a clear error instead of silently leaving an untracked file. The error tells you to either gitignore the path or disable the plugin for the repository (`mngr config set --scope project plugins.claude_subagent_proxy.enabled false`).

## 2026-06-11

The `claude_subagent_proxy` plugin is now **disabled by default** and must be explicitly opted into. It only loads when a config layer sets:

```toml
[plugins.claude_subagent_proxy]
enabled = true
```

This inverts the usual plugin default (load-unless-disabled) because the plugin is very experimental and interferes with a lot of other tooling -- it intercepts Claude Code's built-in `Task` tool. The README documents the new opt-in requirement and behavior.

## 2026-06-10

Raised the stale coverage floor from 66% to 70% to match the coverage CI already measures (~71%).

## 2026-06-09

Updated the destroyed-agent fallback to read the preserved common transcript from its new
location. Preserved Claude sessions now mirror the agent state directory under
`<local_host_dir>/preserved/<agent-name>--<agent-id>/`, so the common transcript is read from
`preserved/<name>--<id>/events/claude/common_transcript/events.jsonl` (via the shared
`get_preserved_agent_dir` helper) instead of the former
`plugin/mngr_claude/preserved_sessions/<name>--<id>/common_transcript/events.jsonl`.

## 2026-06-08

Standardized this plugin's test setup on `register_plugin_test_fixtures(globals())`
instead of `pytest_plugins = ["imbue.mngr.conftest"]`, so HOME isolation is wired
the same single way across all mngr plugins. Internal test-infrastructure change
only; no user-facing behavior change.

- Marked unpublished-on-purpose in `UNPUBLISHED_PACKAGES` (it is an experimental plugin coupled to Claude Code internals), so the release tooling will not offer it for publication. Its stale `imbue-mngr==0.2.5` / `imbue-mngr-claude==0.2.5` pins and the dev-group `imbue-mngr-modal==0.1.0` pin are realigned to current workspace versions so `uv lock` stays solvable. No runtime change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-28

# Release test opts into the pytest config guard

`mngr`'s `is_allowed_in_pytest` config field now defaults to `False`, so a
config loaded during a pytest run must opt in. The release-only
`test_real_claude_subagent` helper hand-rolls its own mngr profile and loads it,
so it now writes `is_allowed_in_pytest = true` into that profile's settings.toml.
Test-only change; no user-facing behavior change.

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-14

- `mngr_claude_subagent_proxy`: typed `subagent_type` (e.g. `imbue-code-guardian:verify-and-fix`) now preserves Claude Code's system-prompt contract.
  - PROXY mode: when the resolver finds an on-disk `.md` definition for the parent's `subagent_type` under `<work_dir>/.claude/agents/`, `~/.claude/agents/`, or `~/.claude/plugins/marketplaces/*/plugins/<plugin>/agents/`, the definition body is prepended to the spawned mngr subagent's prompt file under a labeled section header. Built-in types (`general-purpose`, `Explore`, ...) fall through to the prompt-only path unchanged.
  - DENY mode: the deny reason now appends a one-line pointer at the resolved path so Claude can prepend the body to its own prompt file before running the skill's spawn-and-wait protocol. The base skill-pointer text is unchanged for unresolved / built-in types.
  - The `mngr-subagents` skill documents the typed case (including the v1 limitation that tool restrictions declared in agent-definition frontmatter are not honored -- the spawned mngr subagent inherits the user's full Claude config).

## 2026-05-12

- Added a new `DENY` mode to the `mngr_claude_subagent_proxy` plugin. Configure via `[plugins.claude_subagent_proxy] mode = "DENY"` in `settings.toml`. In `DENY` mode the plugin denies every Claude `Task` tool call with a short skill-pointer reason and instead provisions a `mngr-subagents` Claude skill at `.claude/skills/mngr-subagents/SKILL.md` that teaches the explicit two-command spawn-and-wait protocol (`uv run mngr create ...` followed by `python -m imbue.mngr_claude_subagent_proxy.subagent_wait <slug>`). The historical Haiku-dispatcher proxy path remains the default (`mode = "PROXY"`).
- Both `PROXY` and `DENY` modes now share a label-driven `SessionStart` reaper hook that queries `mngr list` for children whose `mngr_claude_subagent_proxy_parent_id` label matches the parent's `MNGR_AGENT_ID` and destroys any in a terminal state (`DONE` / `STOPPED`). The `PROXY`-only per-agent-plugin-cache Stop-hook guarding moved to a separate `guard_stop_hooks` `SessionStart` hook.
- The `mngr-subagents` skill no longer recommends `--reuse` on `mngr create`. Slug collisions between concurrent `Task` calls now surface as a hard "agent already exists" error instead of silently merging unrelated work; the skill explicitly tells Claude to pick a new slug on collision rather than destroying the existing agent. `PROXY` mode's wait-script still uses `--reuse` because its target names are derived from the unique `tool_use_id` and the only retries are bot-driven on the same id.

Disable the `claude_subagent_proxy` plugin in the project-level `.mngr/settings.toml` so that `uv run mngr create` from this repo does not install the experimental Task-tool proxy hooks into newly provisioned Claude agents.

## 2026-05-11

- New experimental plugin `mngr_claude_subagent_proxy` reroutes Claude
  Code's built-in `Task` (Agent) tool through mngr-managed subagents
  via a Haiku dispatcher. Users can `mngr connect` to the spawned
  subagent and observe its progress; the parent still receives a
  normally-shaped `tool_result`. The wait-script invokes
  `mngr create --type mngr-proxy-child`, tags the child with
  `mngr_claude_subagent_proxy_parent_{name,id}` + `_tool_use_id`
  labels for parent↔child queries via `mngr list --format json`,
  and tails the child's transcript JSONL until a terminal stop
  reason. Project / plugin Stop hooks are auto-guarded with an
  env-conditional `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD` prefix so they
  no-op inside spawned subagents (otherwise an autofix orchestrator
  in the parent will hold its child responsible for the parent's
  uncommitted changes / failing CI). See `libs/mngr_claude_subagent_proxy/README.md`
  for the full architecture, label schema, deferred work, and
  experimental-status banner.
