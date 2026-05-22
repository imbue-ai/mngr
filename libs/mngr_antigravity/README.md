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

`auto_allow_permissions = true` adds `--dangerously-skip-permissions` to the launch command so every tool call is auto-approved without prompting.

`emit_common_transcript = true` (default) starts a background worker that streams agy's per-conversation JSONL transcripts into `events/antigravity/common_transcript/events.jsonl`. `mngr transcript <agent>` reads from there.

## Caveats

- **`agy` PATH shadowing**: if the Antigravity 2.0 desktop app is installed, its bundled `agy` shim can shadow the standalone CLI in `PATH`. Remove the desktop app's `bin/agy` or override `command` with an absolute path to the Go binary.
- **Workspace symlink workaround**: agy refuses to add any path with a dot-prefixed segment as a workspace (logs `Failed to add workspace folder ... is hidden: ignore uri` and falls back to the user's home dir). mngr's `work_dir` lives under `~/.mngr/worktrees/...`, which trips this check. As a workaround, `mngr_antigravity` creates a per-agent symlink at `/tmp/mngr_antigravity_workspaces/<agent_id>` that targets the real `work_dir`, and launches agy with cwd set to the symlink. agy then sees the symlink path as its workspace (`project: using project "/tmp/..."`) and the hidden-path error is suppressed. The symlink is recreated via `ln -sfn` on every launch; `/tmp` wipes are self-repairing. Symlinks aren't pruned when agents are destroyed (they're inert and small). Track [the upstream agy bug report](https://discuss.ai.google.dev/t/add-workspace-rule-failed/114582) for when a flag-level fix lands; we can drop the workaround then.
- **First-launch trust dialog**: each fresh `work_dir` would normally trigger Antigravity's "Do you trust this folder?" gate, which intercepts the first keystroke sent to the tmux pane and breaks `mngr message`. `mngr_antigravity` dismisses the dialog before launch by appending the agent's `work_dir` to `~/.gemini/antigravity-cli/settings.json::trustedWorkspaces`. Because that file is shared user state, the write is gated:
    - `mngr create --yes` (`mngr_ctx.is_auto_approve`) or `auto_dismiss_dialogs = true` on the agent type: silent trust.
    - Interactive shell: mngr prompts via `click.confirm` before writing.
    - Non-interactive shell without either opt-in: provisioning raises `UserInputError`. Re-run with `--yes` or set `auto_dismiss_dialogs = true`.
- **No readiness sentinel**: readiness is signalled purely by polling the rendered TUI banner. Live testing showed agy *loads* `hooks.json` but hook execution is gated behind the `json-hooks-enabled` experiment flag that Google enables per-account; once it ships GA we can re-introduce the sentinel.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
