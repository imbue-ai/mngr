# imbue-mngr-codex

The `codex` agent-type plugin for [`mngr`](https://pypi.org/project/imbue-mngr/):
support for the OpenAI Codex CLI (the Rust `codex` binary) as a first-class mngr agent.

`mngr create my-task codex` launches an interactive Codex TUI agent that mngr can monitor
(RUNNING/WAITING), message, transcript, stop, resume, and isolate per agent.

`mngr stop` then `mngr start` resumes the prior conversation. One `codex` login
authenticates every agent (the per-agent auth is shared with `~/.codex`).

## Configuration

Set fields under an `[agent_types.codex]` table in your mngr config, or pass overrides.

- `model` — model slug to pin (e.g. `"gpt-5.5"`). Default: unset (codex's own default).
- `model_reasoning_effort` — `none|minimal|low|medium|high|xhigh`. Default: unset.
- `sandbox_mode` — `read-only|workspace-write|danger-full-access`. Default: `workspace-write`.
- `auto_allow_permissions` — when `true`, sets `approval_policy = "never"` so codex never
  prompts for tool approval (the sandbox still applies). Default: `false`.
- `config_overrides` — free-form key/values merged last into the per-agent `config.toml`.
- `auto_dismiss_dialogs` — when `true`, trust the repo and allow the hook bypass without
  prompting. Default: `false`.
- `version` — pin the codex CLI version to install (e.g. `"0.139.0"`). When set, installation
  runs `npm i -g @openai/codex@<version>` and provisioning verifies the installed codex matches,
  erroring on a mismatch. A pin also suppresses the provision-time update check (`update_policy`
  is ignored), since updating would defeat the pin. Default: unset (latest).
- `update_policy` — how mngr handles an outdated codex CLI at provision: `AUTO` (run
  `codex update`, no prompt), `ASK` (prompt on an attended local run, else notify), or
  `NEVER` (only notify). An outdated codex still runs. Default: `ASK`. Ignored when
  `version` is pinned.
- `emit_common_transcript` — emit the common-schema transcript. Default: `true`.

### Model note

A ChatGPT-account login rejects some `*-codex` model slugs (e.g. `gpt-5.2-codex`) with a
400 "model is not supported when using Codex with a ChatGPT account": some slugs are
deprecated for ChatGPT subscriptions, and some are gated to the interactive TUI. If your
agent errors on the first message with that 400, set `model` to a slug your account
supports (e.g. `"gpt-5.5"`), or authenticate with an API key (which carries the full
model entitlement).

## Waiting reason

`mngr list` shows a `waiting_reason` for each codex agent:

- `PERMISSIONS` — blocked on a tool-approval dialog, waiting for you to respond.
- `END_OF_TURN` — idle, its turn complete, waiting for your next message.

With `auto_allow_permissions = true` codex never prompts, so an agent never waits with
reason `PERMISSIONS`.

## Limitations

- If you cancel an approval dialog (Esc / "No"), the `waiting_reason` may briefly read
  `PERMISSIONS` rather than `END_OF_TURN` until the next turn completes. The
  `WAITING`/`RUNNING` state itself is always correct.
