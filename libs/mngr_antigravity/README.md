# imbue-mngr-antigravity

Plugin that registers the `antigravity` agent type for mngr.

[Antigravity CLI](https://antigravity.google/docs/cli-overview) is Google's terminal-based AI coding assistant. This plugin runs the CLI (`agy`) as an mngr agent.

## Authentication

Each agent runs `agy` under its own `$HOME` and reads its token from a per-agent path that is, by default, a **symlink to the shared** `~/.gemini/antigravity-cli/antigravity-oauth-token`. Because `agy` writes the token in place, the result is "log in once, anywhere":

- If the shared token already exists, every agent is authenticated with no per-agent login.
- If it doesn't, the first agent you log into writes its token through the symlink to the shared path, authenticating every other agent. Token refreshes propagate the same way.

This works on both Linux and macOS. Set `symlink_oauth_token = false` for full per-agent isolation (each agent authenticates independently).

## Usage

```bash
mngr create my-agent antigravity
```

Pass arguments to the `agy` command with `--`:

```bash
mngr create my-agent antigravity -- --help
```

Each agent gets its own per-agent permissions, model selection, and isolated state (transcripts/conversations), rather than sharing the user's global `~/.gemini`.

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.readonly_agy]
parent_type = "antigravity"
settings_overrides = { permissions = { allow = ["command(git)"], deny = ["command(rm -rf)"], ask = ["command(*)"] }, toolPermission = "proceed-in-sandbox", model = "Gemini 3.5 Flash (Medium)" }
```

Then create agents with your custom type:

```bash
mngr create my-agent readonly_agy
```

### Fields

- `settings_overrides` (dict, default `{}`) — a free-form blob merged last into the per-agent `settings.json`. Common keys:
    - `permissions` — `{allow, deny, ask}`, each a list of `action(target)` resources. Actions: `read_file`, `write_file`, `read_url`, `execute_url`, `command`, `unsandboxed`, `mcp`. Precedence is **Deny > Ask > Allow**. `command(...)` matches a token-prefix/regex with no path scoping; file/url targets must be **canonical** (on macOS `/tmp` -> `/private/tmp`) — a wrong target fails open to Ask.
    - `toolPermission` — the global default mode, e.g. `"proceed-in-sandbox"` or `"request-review"`.
    - `model` — a display name exactly as listed by `agy models`, e.g. `"Gemini 3.5 Flash (Medium)"`.
- `sync_home_settings` (bool, default `true`) — base the per-agent `settings.json` on a copy of the user's real global `~/.gemini/antigravity-cli/settings.json`, with `settings_overrides` layered on top. When `false`, start from an empty base. The copy captures theme/telemetry/trust only; set an agent's model and permission policy explicitly via `settings_overrides`.
- `symlink_oauth_token` (bool, default `true`) — symlink the shared oauth token into each agent's home (so refreshes propagate) or copy it (`false`) for full isolation.
- `auto_allow_permissions` (bool, default `false`) — auto-approve every tool call via agy's `--dangerously-skip-permissions` flag. When combined with a `permissions` policy in `settings_overrides`, skip-permissions wins (the policy is moot).
- `auto_dismiss_dialogs` (bool, default `false`) — silently trust the source repo without prompting (see Trust below).
- `emit_common_transcript` (bool, default `true`) — stream agy's per-conversation transcripts so `mngr transcript <agent>` can read them.

## Conversation resume

`mngr stop` then `mngr start` resumes the agent's prior agy conversation, keeping its full context rather than starting fresh.

## Caveats

- **`agy` PATH shadowing**: if the Antigravity 2.0 desktop app is installed, its bundled `agy` shim can shadow the standalone CLI in `PATH`. Remove the desktop app's `bin/agy` or override `command` with an absolute path to the Go binary.
- **Trust**: agy prompts to trust a folder on first launch. mngr seeds the workspace path into the agent's `settings.json` so it isn't re-prompted. Granting trust is gated: `mngr create --yes` or `auto_dismiss_dialogs = true` trust silently; an interactive shell prompts before writing; a non-interactive shell without either opt-in exits cleanly (re-run with `--yes` or set `auto_dismiss_dialogs = true`).
- **No permission-specific WAITING reason**: agy exposes no permission-dialog hook event, so in supervised mode mngr cannot currently flag *why* an agent is waiting. With `auto_allow_permissions = true` there are no dialogs anyway.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
