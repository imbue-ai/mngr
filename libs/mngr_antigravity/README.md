# imbue-mngr-antigravity

Plugin that registers the `antigravity` agent type for mngr.

[Antigravity CLI](https://antigravity.google/docs/cli-overview) is Google's terminal-based AI coding assistant, the successor to the legacy Gemini CLI. Google announced on 2026-05-19 that Gemini CLI is being replaced by Antigravity CLI, with the legacy request path turning off for paid-tier accounts on 2026-06-18. This plugin lets you run the new CLI (`agy`) as an mngr agent.

## Usage

```bash
mngr create my-agent antigravity
```

Pass arguments to the `agy` command with `--`:

```bash
mngr create my-agent antigravity -- --help
```

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_agy]
parent_type = "antigravity"
cli_args = "--add-dir=/extra/dir"
```

Then create agents with your custom type:

```bash
mngr create my-agent my_agy
```

`auto_allow_permissions = true` auto-approves every tool call without prompting, via agy's `--dangerously-skip-permissions` flag. (It is *not* a hook: agy's documented `PreToolUse` `{"decision": "allow"}` output does not actually gate the `run_command` confirmation dialog -- verified live against agy 1.0.3 -- so the flag is the only reliable mechanism.)

`emit_common_transcript = true` (default) starts a background worker that streams agy's per-conversation JSONL transcripts into `events/antigravity/common_transcript/events.jsonl`. `mngr transcript <agent>` reads from there.

## Conversation resume

Stopping an antigravity agent and starting it again (`mngr stop` / `mngr start`) resumes the agent's prior agy conversation, keeping its full context -- it does not start a fresh conversation. This works automatically; there is no flag to set.

How it works: a `PreInvocation` hook records the agent's active conversation ID (from agy's hook payload) to a per-agent file as the agent works. On restart, the launch command passes the most-recently-active ID to `agy --conversation <id>`. If the conversation has since been deleted from agy's store, the agent launches fresh instead. Because agy stores conversations in a single global location (`~/.gemini/antigravity-cli/`) rather than per-agent, *cloning* an agent does not yet carry the source's conversation forward; that is a separate follow-up.

## Caveats

- **`agy` PATH shadowing**: if the Antigravity 2.0 desktop app is installed, its bundled `agy` shim can shadow the standalone CLI in `PATH`. Remove the desktop app's `bin/agy` or override `command` with an absolute path to the Go binary.
- **Workspace symlink workaround**: agy refuses to add any path with a dot-prefixed segment as a workspace (logs `Failed to add workspace folder ... is hidden: ignore uri` and falls back to the user's home dir). mngr's `work_dir` lives under `~/.mngr/worktrees/...`, which trips this check. As a workaround, `mngr_antigravity` creates a per-agent symlink at `/tmp/mngr_antigravity_workspaces/<agent_id>` that targets the real `work_dir`, and launches agy with cwd set to the symlink. agy then sees the symlink path as its workspace (`project: using project "/tmp/..."`) and the hidden-path error is suppressed. The symlink is recreated via `ln -sfn` on every launch; `/tmp` wipes are self-repairing. Symlinks aren't pruned when agents are destroyed (they're inert and small). Track [the upstream agy bug report](https://discuss.ai.google.dev/t/add-workspace-rule-failed/114582) for when a flag-level fix lands; we can drop the workaround then.
- **First-launch trust dialog**: each fresh `work_dir` would normally trigger Antigravity's "Do you trust this folder?" gate, which intercepts the first keystroke sent to the tmux pane and breaks `mngr message`. `mngr_antigravity` dismisses the dialog before launch by appending the agent's `work_dir` to `~/.gemini/antigravity-cli/settings.json::trustedWorkspaces`. Because that file is shared user state, the write is gated:
    - `mngr create --yes` (`mngr_ctx.is_auto_approve`) or `auto_dismiss_dialogs = true` on the agent type: silent trust.
    - Interactive shell: mngr prompts via `click.confirm` before writing.
    - Non-interactive shell without either opt-in: provisioning raises `UserInputError`. Re-run with `--yes` or set `auto_dismiss_dialogs = true`.
- **Hooks (lifecycle marker + conversation capture)**: `mngr_antigravity` provisions a per-agent `hooks.json` and points agy at it with `--add-dir` (through a `/tmp` symlink, since agy rejects the dotted state-dir path -- same hidden-path rule as the workspace symlink). A `PreInvocation`/`Stop` pair maintains an `active` marker so the agent reports RUNNING while working and WAITING when idle (agy writes no such marker on its own). A second `PreInvocation` handler records the active conversation ID (which drives conversation resume and transcript scoping; see "Conversation resume" above). agy delivers the hook payload to each handler independently, so the two handlers don't contend for stdin. Verified live against agy 1.0.3 that hooks load and execute. Note: the in-TUI `/hooks` command writes to `~/.gemini/antigravity-cli/hooks.json`, which the hook *execution* engine never runs -- that path is loaded only for the TUI's display/management view, while only `~/.gemini/config/hooks.json` and per-workspace `.agents/hooks.json` are actually executed ([antigravity-cli#49](https://github.com/google-antigravity/antigravity-cli/issues/49)). mngr writes its own per-agent file under an `--add-dir` path and never relies on the TUI.
- **No readiness sentinel**: readiness is still signalled purely by polling the rendered TUI banner. agy's hook events are execution-loop events (`PreToolUse`/`PostToolUse`/`PreInvocation`/`PostInvocation`/`Stop`) with no "input prompt ready" analog, so they can't replace the banner poll.
- **No permission-specific WAITING reason**: agy exposes no permission-dialog hook event, and no hook fires while the agent is blocked at the dialog, so mngr can't currently flag *why* an agent is waiting (as `mngr_claude` does). With `auto_allow_permissions = true` there are no dialogs anyway; surfacing a permission-WAITING reason in supervised mode is left for a follow-up.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
