# imbue-mngr-codex

The `codex` agent-type plugin for [`mngr`](https://pypi.org/project/imbue-mngr/): real
support for the OpenAI Codex CLI (the Rust `codex` binary) as a first-class mngr agent,
on par with the `claude` and `antigravity` agent types.

`mngr create my-task codex` launches an interactive Codex TUI agent that mngr can monitor
(RUNNING/WAITING), message, transcript, stop, resume, and isolate per agent.

## How it works

Codex is architecturally the closest CLI to Claude Code, so this plugin follows the
`mngr_claude` shape. See `specs/agent-plugin-parity/codex-investigation.md` in the monorepo
for the full, source-verified investigation behind each decision.

- **Per-agent isolation via `CODEX_HOME`.** Codex resolves its whole
  config/auth/session/hook tree from `CODEX_HOME` (default `~/.codex`). Each agent gets its
  own `CODEX_HOME` under the agent state dir, injected only on the codex process
  (`env CODEX_HOME=...`), leaving the user's real `$HOME` untouched. No `$HOME` relocation,
  no workspace symlink (codex accepts the dotted `~/.mngr/...` cwd).
- **Shared auth.** The per-agent `auth.json` is a symlink to the user's shared
  `~/.codex/auth.json`. Codex writes that file in place and reloads-before-refreshing, so
  one login authenticates every agent and token refreshes propagate. `config.toml` pins
  `cli_auth_credentials_store = "file"` so codex never falls back to a keyring entry keyed
  by the per-agent `CODEX_HOME` path (which would defeat sharing).
- **Lifecycle marker (RUNNING/WAITING), subagent-aware.** Codex subagents (the multi-agent
  `spawn_agent` feature) run *asynchronously* -- the root agent's `Stop` hook fires while
  subagents are still running, with no `fullyIdle` signal. So mngr does not just clear the
  marker on `Stop`; it recomputes an `active` marker that is present while either the root
  turn is running (`codex_root_active`, set on `UserPromptSubmit`, cleared on the root
  `Stop`) or any subagent is in flight (one file per `agent_id` under `codex_subagents/`,
  maintained by the `SubagentStart`/`SubagentStop` hooks). The recompute runs under a portable
  `mkdir` lock so a concurrent root `Stop` and final `SubagentStop` can't strand it. A
  recorded root `session_id` guards against a nested `codex` process sharing the same
  `CODEX_HOME`. (Backgrounded OS processes the agent launches are not tracked -- codex emits
  no hook for them.)
- **Conversation resume.** `mngr stop` then `mngr start` resumes the prior conversation: the
  hook records the root `session_id`, and the launch command shell-evaluates
  `codex resume <id>` (codex's session JSONL survives the hard kill `mngr stop` performs).
- **Transcripts.** The native rollout JSONL is streamed verbatim to the raw transcript and
  converted into mngr's agent-agnostic common transcript that `mngr transcript` reads.
- **Trust & hook bypass (consent-gated).** mngr seeds the work dir as a trusted project to
  skip codex's folder-trust dialog, and passes `--dangerously-bypass-hook-trust` so its own
  lifecycle hooks run. Because trusting the workspace also lets codex load repo-local
  `.codex/hooks.json`, that bypass is consent-gated together with workspace trust: mngr
  prompts before trusting (or use `mngr create --yes` / `auto_dismiss_dialogs = true`).

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
- `emit_common_transcript` — emit the common-schema transcript. Default: `true`.

### Model note

Codex picks the account's default model, and a **ChatGPT-account login rejects some
`*-codex` model slugs** (e.g. `gpt-5.2-codex`) with a 400 "model is not supported when using
Codex with a ChatGPT account". If your agent errors on the first message with that, set
`model` to a model your account supports (e.g. `"gpt-5.5"`).

## Not yet implemented

Relative to `mngr_claude`, these are not yet ported (tracked for follow-up): session
preservation on destroy, deploy/scheduling contributions, field generators (`waiting_reason`),
the streaming snapshot, and installation/version management. A future `headless_codex` subtype
could drive `codex app-server` (JSON-RPC) for clean synchronous lifecycle/stream events,
directly paralleling `mngr_claude`'s `headless_claude`.
