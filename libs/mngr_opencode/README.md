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
- **Transcripts**: the same plugin writes the raw transcript; a background
  converter turns it into the common format `mngr transcript` reads.
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
| `cli_args` | `()` | Extra arguments appended to the `opencode` command. |
| `config_overrides` | `{}` | Key/value blob merged last into the per-agent `opencode.json` (e.g. `model`, the `permission` policy block). |
| `sync_global_config` | `true` | Base the per-agent `opencode.json` on a copy of the user's `~/.config/opencode/opencode.json`. |
| `symlink_auth` | `true` | Symlink the per-agent `auth.json` to the shared one (one login authenticates all agents). Set `false` for full isolation. |
| `auto_allow_permissions` | `false` | Inject a wildcard allow into the per-agent permission policy (auto-approve everything not explicitly denied). |
| `emit_common_transcript` | `true` | Emit the common transcript that `mngr transcript` reads. |

A model must be resolvable for the agent to run unattended -- set it via
`config_overrides.model`, your global `~/.config/opencode/opencode.json`, or an
authenticated provider's default.

## Not yet implemented

Carried gaps (shared with `mngr_antigravity`): session preservation on destroy,
scheduled-deploy contributions, the `waiting_reason` listing field, the live
streaming snapshot, and clone carrying the source conversation forward.

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md)
for more details.
