# imbue-mngr-opencode

Plugin that registers the `opencode` agent type for mngr.

[OpenCode](https://github.com/sst/opencode) is an open-source terminal-based AI
coding agent. This plugin runs it as a first-class mngr agent: an interactive
TUI with RUNNING/WAITING lifecycle reporting, conversation resume across
stop/start, `mngr transcript` support, and per-agent config/credential
isolation.

## Usage

```bash
mngr create my-agent opencode
```

Pass arguments straight through to the `opencode` command with `--`:

```bash
mngr create my-agent opencode -- --model anthropic/claude-sonnet-4-5
```

`mngr stop` then `mngr start` resumes the agent's conversation; `mngr transcript
<agent>` prints the conversation; `mngr list` reports the agent as RUNNING while
it works and WAITING when it is idle.

## How it works

OpenCode is a **client-server** app (a server owns the sessions and event bus;
TUI / CLI / HTTP clients talk to it) with SQLite-backed sessions and no POSIX-sh
hook mechanism. mngr leans into that shape:

- **The agent runs as a server + an attached TUI.** Each agent's tmux pane runs
  a launch script that starts a headless `opencode serve` plus an `opencode
  attach` TUI client. `mngr connect` shows the attached client; the server owns
  the session.
- **Messages are sent via the server's HTTP API**, not by typing into the TUI:
  `mngr message` POSTs the prompt to the agent's server (`prompt_async`), and the
  attached client renders it — so the conversation is fully visible while sending
  stays robust (no keystroke races, no screen-scraping).
- **Isolation** is via `OPENCODE_CONFIG_DIR` (a per-agent config dir holding
  `opencode.json` + an auto-loaded plugin) and `XDG_DATA_HOME` (a per-agent data
  dir holding the session db, `auth.json`, storage, and logs), injected only on
  the OpenCode processes. No `$HOME` relocation.
- **Lifecycle (RUNNING/WAITING)** is maintained by a small in-process TypeScript
  plugin that watches the server's event bus and touches/removes the mngr
  `active` marker (it runs only in the server process). It is subagent-aware: the
  marker clears only when the *root* session goes idle, so spawning task-tool
  subagents keeps the agent RUNNING until the whole turn finishes.
- **`waiting_reason`** (shown by `mngr list`) reports *why* a WAITING agent is
  blocked, as the claude and codex agent types do. When a tool blocks on an
  approval prompt (an `ask` permission policy), opencode emits `permission.asked`;
  the plugin tracks pending prompts and keeps a `permissions_waiting` marker, which
  promotes the agent to WAITING and surfaces a `PERMISSIONS` reason. An idle agent
  whose turn is complete reports `END_OF_TURN`. Unlike codex (whose hook model fires
  nothing when a dialog is cancelled, briefly mislabeling the reason), answering or
  cancelling an opencode prompt clears the marker promptly: a denial emits
  `permission.replied` *and* `session.idle`, and an abort emits `session.idle` —
  each clears it (verified live against opencode 1.17.7).
- **Transcripts**: the same plugin writes the raw transcript and, on session
  idle, rebuilds the common-format transcript `mngr transcript` reads — both
  in-process, no background converter or supervisor.
- **Auth**: the per-agent `auth.json` symlinks to the user's shared
  `~/.local/share/opencode/auth.json`, so one `opencode auth login` (in any
  agent) authenticates them all.

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_opencode]
parent_type = "opencode"
auto_allow_permissions = true

[agent_types.my_opencode.config_overrides]
model = "anthropic/claude-sonnet-4-5"
```

Then create agents with your custom type:

```bash
mngr create my-agent my_opencode
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `cli_args` | `()` | Extra arguments forwarded to the `opencode attach` (TUI) client. |
| `config_overrides` | `{}` | Key/value blob merged last into the per-agent `opencode.json` (e.g. `model`, the `permission` policy block). |
| `sync_global_config` | `true` | Base the per-agent `opencode.json` on a copy of the user's `~/.config/opencode/opencode.json`. |
| `symlink_auth` | `true` | Symlink the per-agent `auth.json` to the shared one (one login authenticates all agents). Set `false` for full isolation. |
| `auto_allow_permissions` | `false` | Inject a wildcard allow into the per-agent permission policy (auto-approve everything not explicitly denied). |
| `emit_common_transcript` | `true` | Emit the common transcript that `mngr transcript` reads. |

## Choosing the model

The model is read by the agent's OpenCode server from its per-agent
`opencode.json`, so it is set through config (format: `provider/model`; list
options with `opencode models`). Three ways, highest precedence first:

1. **Per agent-type** -- `config_overrides.model` on an `opencode` variant (the
   TOML example above). Applies to every agent of that type.
2. **On the `mngr create` command line** -- mngr's generic per-invocation
   override reaches right into `config_overrides`:

   ```bash
   mngr create my-agent opencode -S agent_types.opencode.config_overrides.model=anthropic/claude-sonnet-4-5
   ```

3. **Globally** -- the `"model"` in your `~/.config/opencode/opencode.json`,
   inherited when `sync_global_config` is `true` (the default).

Notes:

- If the model's provider isn't authenticated, **OpenCode silently falls back to
  a free model** -- so if you set, say, `anthropic/claude-sonnet-4-5` but the
  agent shows a free "OpenCode Zen" model, run `opencode auth login` and
  create/restart the agent. (The free OpenCode-Zen models need no auth.)
- Passing the model after `--` (`mngr create ... opencode -- --model X`) does
  **not** work: `--` args go to the `opencode attach` client, which has no
  `--model` flag; the model is used by the `serve` process, from `opencode.json`.

## Not yet implemented

Carried gaps (shared with `mngr_antigravity`): session preservation on destroy,
scheduled-deploy contributions, the live streaming snapshot, and clone carrying
the source conversation forward.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md)
for more details.
