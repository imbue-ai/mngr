# Unabridged Changelog - mngr_claude_subagent_proxy

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_claude_subagent_proxy/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
