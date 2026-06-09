# mngr-pi-coding

Plugin that registers the `pi-coding` agent type for mngr.

[Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent) is a minimal terminal coding harness. This plugin runs it as a fully-featured mngr agent: lifecycle-aware (RUNNING vs WAITING), transcript-capturing, and resumable across restarts.

## Usage

```bash
mngr create my-agent pi-coding
```

Pass arguments to the pi command with `--` (e.g. to pick a provider/model):

```bash
mngr create my-agent pi-coding -- --provider anthropic --model claude-haiku-4-5
```

Pi needs a provider credential. The simplest path is to set an API key in your
environment and pass it through, e.g. `mngr create my-agent pi-coding --pass-env ANTHROPIC_API_KEY`,
or run `pi` once and use `/login` to populate `~/.pi/agent/auth.json` (which the
plugin shares into each agent).

## What you get

- **Per-agent isolation.** Each agent gets its own pi config dir via
  `PI_CODING_AGENT_DIR` (`$MNGR_AGENT_STATE_DIR/plugin/pi_coding/`), so settings,
  sessions, and state never collide. Your `~/.pi/agent/` `auth.json`, `settings.json`,
  and resource dirs (`skills`, `prompts`, `extensions`, `themes`) are shared in
  (symlinked locally, copied to remote hosts). `auth.json` is symlinked, so a
  `/login` or token refresh in any agent propagates to the rest.
- **RUNNING vs WAITING.** `mngr list` shows whether the agent is mid-turn or idle,
  and stays correct when the agent runs a nested `pi` via its bash tool.
- **Transcripts.** `mngr transcript my-agent` renders the conversation. A raw pi
  message stream is also captured under the agent state dir.
- **Resume.** `mngr stop` then `mngr start` continues the same pi session with full
  context.

These are powered by a small mngr-owned pi extension that the plugin provisions and
loads with `pi -e`; pi has no shell-hook surface, so its TypeScript extension API is
the lever for lifecycle, readiness, and transcript signalling.

## Configuration

Define a custom variant in your mngr config (`mngr config edit`):

```toml
[agent_types.my_pi]
parent_type = "pi-coding"
cli_args = "--provider anthropic --model claude-haiku-4-5"
```

Then create agents with your custom type:

```bash
mngr create my-agent my_pi
```

Tunables on the `pi-coding` agent type:

| Setting | Default | Description |
|---|---|---|
| `command` | `pi` | The pi command to run. |
| `sync_auth` | `true` | Share `~/.pi/agent/auth.json` into the per-agent dir. |
| `sync_home_settings` | `true` | Share `settings.json` and resource dirs into the per-agent dir. |
| `check_installation` | `true` | Verify pi is installed (and install on remote hosts when allowed). |
| `resume_session` | `true` | Resume this agent's pi session on start, so stop/start keeps context. |
| `emit_common_transcript` | `true` | Emit the transcript `mngr transcript` reads. |
| `emit_raw_transcript` | `true` | Capture the raw pi message stream. |

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
