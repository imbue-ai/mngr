# mngr-pi-coding

Plugin that registers the `pi-coding` agent type for mngr.

[Pi](https://github.com/earendil-works/pi/tree/main/packages/coding-agent) is a minimal terminal coding harness (published on npm as `@earendil-works/pi-coding-agent`). This plugin runs it as a fully-featured mngr agent: lifecycle-aware (RUNNING vs WAITING), transcript-capturing, and resumable across restarts.

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

> **Tool permissions.** pi runs tools -- including shell commands -- without a confirmation
> gate: it has no built-in approval prompts, and mngr adds none. To restrict an agent, use
> pi's own `--tools <allowlist>` via `cli_args`, or run untrusted work in a sandbox.

## What you get

- **Per-agent isolation.** Each agent gets its own pi config dir, so settings,
  sessions, and state never collide. Your `~/.pi/agent/` auth and settings are
  shared in, so a `/login` or token refresh in any agent propagates to the rest.
- **RUNNING vs WAITING.** `mngr list` shows whether the agent is mid-turn or idle.
- **Transcripts.** `mngr transcript my-agent` renders the conversation.
- **Resume.** `mngr stop` then `mngr start` continues the same pi session with full
  context.

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
| `auto_dismiss_dialogs` | `false` | Trust the workspace without prompting (suppress pi's "Trust project folder?" dialog). Also implied by `mngr create --yes`. |

See the [mngr agent types documentation](https://github.com/imbue-ai/mngr/blob/main/libs/mngr/docs/concepts/agent_types.md) for more details.
