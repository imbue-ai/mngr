# imbue-mngr-antigravity

Plugin that registers the `antigravity` agent type for mngr.

[Antigravity CLI](https://antigravity.google/docs/cli-overview) is Google's terminal-based AI coding assistant, the successor to the legacy Gemini CLI. Google announced on 2026-05-19 that Gemini CLI is being replaced by Antigravity CLI, with the legacy request path turning off for paid-tier accounts on 2026-06-18. This plugin lets you run the new CLI (`agy`) as an mngr agent.

## Authentication

Each agent runs `agy` under its own `$HOME` and reads its token from `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`. By default mngr creates that per-agent token as a **symlink to the shared** `~/.gemini/antigravity-cli/antigravity-oauth-token` — even when the shared token doesn't exist yet (a dangling symlink). Because `agy` writes the token **in place**, the result is "log in once, anywhere":

- If the shared token already exists, every agent's symlink resolves to it → agents are authenticated with **no per-agent login**.
- If it doesn't, the **first** agent you log into writes its token *through* the symlink to the shared path, which immediately authenticates every other agent pointing at it. Token refreshes propagate the same way.

This works on both Linux (no keychain — the file token is native) and macOS (where `agy` stores the token in the login keychain, which a relocated per-agent `$HOME` can't reliably read, so the file token is the cross-agent mechanism there too). Set `symlink_oauth_token = false` for full per-agent isolation (each agent authenticates independently; no sharing or propagation).

> **macOS popup during login is expected and harmless.** When you sign in to an agent on macOS you may see a system dialog: *"A keychain cannot be found to store \"antigravity.\""* Because each agent runs under a relocated `$HOME`, agy has no per-agent keychain to write to, so it falls back to writing the **file token** — which is exactly the mechanism mngr relies on (the file token is then shared across agents via the symlink, as above). Just dismiss the dialog; authentication still completes and propagates normally.

## Usage

```bash
mngr create my-agent antigravity
```

Pass arguments to the `agy` command with `--`:

```bash
mngr create my-agent antigravity -- --help
```

## Per-agent isolation

Each `antigravity` agent runs `agy` under its own `$HOME` (at `<agent_state_dir>/plugin/antigravity/home/`). `agy` resolves its entire config/permission/auth/session tree from `$HOME/.gemini` and has no config-dir override env var, so relocating `$HOME` is the only lever that gives an agent its own `settings.json`. This delivers, per agent:

- **Permissions** -- a per-agent allow/deny/ask policy (see below), instead of the all-or-nothing `--dangerously-skip-permissions`.
- **Model** -- a per-agent model selection.
- **Isolated state** -- the agent's own transcripts/conversations rather than sharing the user's global `~/.gemini`.

This is unconditional: there is no "isolated vs non-isolated" mode. Whether an agent is locked down or open is purely *data* -- whether its `settings.json` carries a `permissions` block. Heavy caches (`ms-playwright-go` browser binaries) are shared across agents by symlinking each agent's home cache to the user's real host cache, so they are downloaded once.

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.readonly_agy]
parent_type = "antigravity"
settings_overrides = { permissions = { allow = ["command(git)"], deny = ["command(rm -rf)"], ask = ["command(*)"] }, toolPermission = "proceed-in-sandbox", model = "Gemini 3.5 Flash (Medium)" }
```

To merge onto the base rather than assign, declare the intent in `__mngr_merge` (see the `settings_overrides` field below):

```toml
[agent_types.readonly_agy.settings_overrides.permissions]
allow = ["command(git)"]
[agent_types.readonly_agy.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"   # or "assign"
```

Then create agents with your custom type:

```bash
mngr create my-agent readonly_agy
```

### Fields

- `settings_overrides` (dict, default `{}`) -- a free-form blob merged last into the per-agent `settings.json` (mirrors `mngr_claude`'s field of the same name). Merge intent is declared in a top-level `__mngr_merge` map (dotted key path -> operator), which vanilla agy ignores: a bare key **assigns** (guarded so it errors rather than silently dropping a non-empty list/dict/set from the base, printing the exact `__mngr_merge` patch to add), `"extend"` **merges** onto the base (list concat / set union / recursive dict merge), and `"assign"` replaces without the guard. The `__extend`/`__assign` suffixes are rejected here (they would leak into `settings.json` as keys agy can't read). Common keys:
    - `permissions` -- `{allow, deny, ask}`, each a list of `action(target)` resources. Actions: `read_file`, `write_file`, `read_url`, `execute_url`, `command`, `unsandboxed`, `mcp`. Precedence is **Deny > Ask > Allow**. `command(...)` matches a token-prefix/regex with no path scoping; file/url targets must be **canonical** (on macOS `/tmp` -> `/private/tmp`) -- a wrong target fails open to Ask rather than erroring.
    - `toolPermission` -- the global default mode, e.g. `"proceed-in-sandbox"` or `"request-review"`.
    - `model` -- a display name exactly as listed by `agy models`, e.g. `"Gemini 3.5 Flash (Medium)"`.
- `sync_home_settings` (bool, default `true`) -- base the per-agent `settings.json` on a copy of the user's real `~/.gemini/antigravity-cli/settings.json`, with `settings_overrides` layered on top. When `false`, start from an empty base. This is a data-source choice, not a separate code path. Note that this copies only the **global** `settings.json` scope, which in practice holds theme/telemetry/trust; it does **not** capture the user's model, permission grants, or behavioral policies (`fileAccessPolicy`/`internetPolicy`/etc.), which agy persists in *other* scopes (`config/config.json` `userSettings`, per-project `config/projects/<uuid>.json`) that this copy intentionally does not read -- importing the user's grants would weaken per-agent isolation. Set an agent's model and permission policy explicitly via `settings_overrides`.
- `symlink_oauth_token` (bool, default `true`) -- symlink the shared oauth token into each agent's home (so refreshes propagate) or copy it (`false`) for full isolation.
- `auto_allow_permissions` (bool, default `false`) -- auto-approve every tool call via agy's `--dangerously-skip-permissions` flag. (It is *not* a hook: agy's documented `PreToolUse` `{"decision": "allow"}` output does not gate the `run_command` confirmation dialog -- verified live against agy 1.0.3.) When combined with a `permissions` policy in `settings_overrides`, skip-permissions wins (the policy is moot), matching `mngr_claude`.
- `auto_dismiss_dialogs` (bool, default `false`) -- silently trust the source repo without prompting (see Trust below).
- `update_policy` (`AUTO`/`ASK`/`NEVER`, default unset) -- govern agy's background self-updater. `NEVER` sets `AGY_CLI_DISABLE_AUTO_UPDATE=true` in the agent environment so the installed build stays put; `AUTO` leaves the self-updater enabled; `ASK` behaves like `AUTO` (agy has no interactive update flow). Unset resolves to `NEVER` (auto-update disabled) -- set `AUTO` to leave agy's self-updater enabled. Note: agy has **no** version-pinning capability -- Google's installer always installs the latest build (no version argument or env var), so there is no `version` field; use `update_policy = "NEVER"` (the default) to freeze the installed build.
- `emit_common_transcript` (bool, default `true`) -- start a background worker that streams agy's per-conversation JSONL transcripts into `events/antigravity/common_transcript/events.jsonl`. `mngr transcript <agent>` reads from there.

## Conversation resume

Stopping an antigravity agent and starting it again (`mngr stop` / `mngr start`) resumes the agent's prior agy conversation, keeping its full context rather than starting fresh. This is automatic; there is no flag to set. The conversation resumed is the agent's *main* (root) one, tracked in the per-agent `root_conversation` file (written by the `statusLine` command, whose payload always reports the root `conversation_id`). It is deliberately not read from the conversation-ids file, whose entries also include the agent's subagents. (*Cloning* an agent does not yet carry the source's conversation forward -- that is a separate follow-up.)

## Caveats

- **`agy` PATH shadowing**: if the Antigravity 2.0 desktop app is installed, its bundled `agy` shim can shadow the standalone CLI in `PATH`. Remove the desktop app's `bin/agy` or override `command` with an absolute path to the Go binary.
- **Workspace symlink workaround**: agy refuses to add any path with a dot-prefixed segment as a workspace (logs `Failed to add workspace folder ... is hidden: ignore uri` and falls back to the user's home dir). mngr's `work_dir` lives under `~/.mngr/worktrees/...`, which trips this check. As a workaround, `mngr_antigravity` creates a per-agent symlink at `/tmp/mngr_antigravity_workspaces/<agent_id>` that targets the real `work_dir`, and launches agy with cwd set to the symlink. (HOME relocation does *not* change this: agy accepts a hidden *config* dir, just not a hidden *workspace*.) The symlink is recreated via `ln -sfn` on every launch; `/tmp` wipes are self-repairing. Symlinks aren't pruned when agents are destroyed (they're inert and small). Track [the upstream agy bug report](https://discuss.ai.google.dev/t/add-workspace-rule-failed/114582) for when a flag-level fix lands; we can drop the workaround then.
- **Trust**: agy suppresses its first-launch "Do you trust this folder?" dialog for any path in its `settings.json` `trustedWorkspaces`. mngr seeds the agent's transient workspace path into the *per-agent* `settings.json` (the running agy exact-matches its cwd), and additionally persists the durable **source-repo path** into the user's *global* `~/.gemini/antigravity-cli/settings.json` so trust isn't re-prompted across agents/worktrees of the same repo. The transient per-agent path is never written to the global file. Granting trust is gated (mngr never silently runs an agent on untrusted code):
    - `mngr create --yes` (`mngr_ctx.is_auto_approve`) or `auto_dismiss_dialogs = true`: silent trust.
    - Interactive shell: mngr prompts via `click.confirm` before writing.
    - Non-interactive shell without either opt-in, or a declined prompt: provisioning exits cleanly (`SystemExit`). Re-run with `--yes` or set `auto_dismiss_dialogs = true`.
- **Lifecycle via `statusLine`**: agy invokes a configured `statusLine` command on **every** agent-state change, piping a JSON payload (`agent_state`, `conversation_id`, `model`, `context_window`, ...) on stdin and rendering the command's stdout in the prompt's status row. `mngr_antigravity` seeds a mngr-owned `statusLine` into the per-agent `settings.json` pointing at `statusline.sh`, which is the single source of truth for the agent's lifecycle. On each invocation it: (1) maintains the `active` marker that drives RUNNING (busy) vs WAITING (idle) -- active **iff** `agent_state` is not in `{idle, initializing, authenticating, ""}` (a denylist, so any present/future busy state counts as RUNNING); (2) records the root `conversation_id` in `root_conversation` for resume (the payload always reports the root, even while a subagent runs); and (3) fires `tmux wait-for -S "mngr-submit-<session>"`, the signal `mngr message` waits on to confirm the message was accepted. Crucially, agy's top-level `agent_state` already **aggregates subagent activity**: it stays `working` continuously while a subagent runs and returns to `idle` only once root + subagents are all done (verified live against agy 1.0.6/1.0.7: 75 consecutive `working` samples spanning a ~29s subagent run, zero mid-turn `idle` blips), so a single `agent_state` check captures the whole-turn busy/idle state without any per-conversation bookkeeping. This `statusLine` is mngr-owned and applied **last** (after `settings_overrides`), because lifecycle correctness depends on it -- the agy `statusLine` must be mngr's. mngr's use of `statusLine` is **lifecycle-only**: the side-effects (marker, root conversation, submission signal) are the point, and agy already shows working/idle in its own UI, so `statusline.sh` prints **nothing of its own** -- the status row looks exactly as it would without mngr. agy allows only one `statusLine` command, so a user's own (in `settings_overrides` or the synced global settings) can't be the agy `statusLine`; instead it is **composed** -- the provisioner records its command and `statusline.sh` runs it (with the same payload on stdin, exactly as agy would deliver it) and emits **only its output**, so the user's statusline renders verbatim while mngr stays invisible. Only a runnable `{"type": "command", "command": "<shell>"}` block can be composed; a different shape is dropped with a warning.
- **Hooks (conversation capture)**: `mngr_antigravity` also writes a per-agent `hooks.json` to `$HOME/.gemini/config/hooks.json` (in the agent's relocated home); agy executes it directly with no trust prompt and no `--add-dir`. The lone hook is a single `PreInvocation` handler running `capture_conversation_id.sh`, which records every conversation ID the agent touches -- the root's **and its subagents'** -- into the conversation-ids file that scopes transcript streaming (the *set* of conversations to tail). This hook remains (rather than folding into `statusLine`) precisely because the `statusLine` payload only ever reports the **root** conversation, so subagent ids surface only here. Note: the in-TUI `/hooks` command writes to `~/.gemini/antigravity-cli/hooks.json`, which the hook *execution* engine never runs -- that path is loaded only for the TUI's display, while only `~/.gemini/config/hooks.json` and per-workspace `.agents/hooks.json` are actually executed ([antigravity-cli#49](https://github.com/google-antigravity/antigravity-cli/issues/49)).
- **No readiness sentinel**: readiness is signalled purely by polling the rendered TUI banner. The `statusLine` `agent_state` is about the agent loop and can be `idle` *before* the input row is drawn, so it can't replace the banner poll -- which gates "input row drawn and able to receive a paste", the correct precondition for sending input.
- **No permission-specific WAITING reason**: agy exposes no permission-dialog hook event, and no hook fires while the agent is blocked at the dialog, so mngr can't currently flag *why* an agent is waiting (as `mngr_claude` does). With `auto_allow_permissions = true` there are no dialogs anyway; surfacing a permission-WAITING reason in supervised mode is left for a follow-up.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
