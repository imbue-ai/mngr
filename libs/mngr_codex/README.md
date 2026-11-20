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
- `fall_back_to_account_default_model` — when `true` and `model` is set, a new thread whose
  account does not offer the pinned model starts on the account's default model instead
  (mngr checks the account's model list first, and logs the substitution). It applies only to
  threads mngr starts itself on a local host; a thread that already exists keeps its model.
  Default: `false`.
- `model_reasoning_effort` — `none|minimal|low|medium|high|xhigh`. Default: unset.
- `sandbox_mode` — `read-only|workspace-write|danger-full-access`. Default: `workspace-write`.
- `auto_allow_permissions` — when `true`, sets `approval_policy = "never"` so codex never
  prompts for tool approval (the sandbox still applies). Default: `false`.
- `config_overrides` — free-form key/values merged last into the per-agent `config.toml`. Each
  config layer adds its keys to those of the layers below it. To merge a nested table that a lower
  layer also set, write its key with `__extend` (e.g. `features__extend`); a plain nested table that
  drops one of the lower table's keys fails to load with a settings-narrowing error.
- `auto_dismiss_dialogs_at_startup` — when `true`, trust the repo and allow the hook bypass without
  prompting. Default: `false`.
- `version` — pin the codex CLI version to install (e.g. `"0.139.0"`). When set, installation
  runs `npm i -g @openai/codex@<version>` and provisioning verifies the installed codex matches
  (whether or not `check_installation` is set). A pin also suppresses the provision-time update
  check (`update_policy` is ignored), since updating would defeat the pin. Default: unset (latest).
- `version_mismatch` — what provisioning does when the installed codex does not report the pinned
  `version`: `ERROR` fails the create, `WARN` logs a warning and continues on the installed
  binary. Default: `ERROR`.
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
model entitlement). To pin a model for every account while tolerating ones whose plan lacks
it, also set `fall_back_to_account_default_model = true`.

## State

codex's hooks and mngr's transcript streamer keep the agent's turn marker on its host, so
`mngr list` reads a codex agent's state on any host. A turn reads `RUNNING` from codex's
`UserPromptSubmit` hook, or within a second from the rollout's record that a task started,
which also covers turns that hook does not report (a compaction, a `!` shell command typed in
the TUI). It ends at codex's `Interrupt` hook, or up to about a second after codex's `Stop`
hook, when the streamer reads the rollout's record of the turn's end. That record also ends a
turn that fails, which runs no `Stop` hook, and comes only after the continuation of a turn
that a `Stop` hook in the repo's `.codex/hooks.json` keeps going (exit code 2), so such a turn
reads `RUNNING` throughout.

`mngr list` shows a `waiting_reason` for each codex agent:

- `PERMISSIONS` — blocked on a tool-approval dialog or on a question codex asked you (plan
  mode), waiting for you to respond.
- `END_OF_TURN` — idle, its turn complete, waiting for your next message.

With `auto_allow_permissions = true` codex never asks for an approval.

## Limitations

- An approved command keeps reading `WAITING` with reason `PERMISSIONS` until it finishes,
  and a declined one until the turn's next tool finishes or the turn ends: codex runs no hook
  when you answer.
- Only the root thread's turn, approvals and questions count. A subagent still working after
  the root turn ended reads `WAITING`, and a subagent's approval or question is not shown.
- codex runs mngr's hooks only once they are trusted. mngr trusts them when it creates an
  agent on a local host. On a remote host, codex shows "Hooks need review" in the agent's
  terminal after its first restart. Until someone picks "Trust all" there, turns still read
  `RUNNING` and `WAITING` from the rollout, but an approval or a question reads `RUNNING`.
- An agent created before the turn hook reads its turn from codex's app-server instead, on a
  local host only, until it is re-provisioned (`mngr create <name> --reuse --update`).
