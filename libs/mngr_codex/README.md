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
Codex with a ChatGPT account". Two distinct things cause this:

- **Deprecation:** `gpt-5.2` / `gpt-5.2-codex` / `gpt-5.3-codex` have been sunset for ChatGPT
  subscriptions (OpenAI's announcement points those users to the API). These fail on a ChatGPT
  plan in *every* mode, including the TUI.
- **Run-mode entitlement:** the backend gates some `*-codex` models by the `originator` HTTP
  header (i.e. the client identity). The interactive TUI presents as `codex-tui` and is
  allowed; `codex exec` presents as `codex_exec` and is denied. (See the app-server note below
  for why this matters and why we do *not* spoof the TUI identity.)

If your agent errors on the first message with the "not supported" 400, set `model` to a model
your account supports (e.g. `"gpt-5.5"`), or authenticate with an API key (which carries the
full model entitlement).

## Not yet implemented

Relative to `mngr_claude`, these are not yet ported (tracked for follow-up): session
preservation on destroy, deploy/scheduling contributions, field generators (`waiting_reason`),
the streaming snapshot, and installation/version management.

## Future direction: an app-server-backed agent variant

This agent drives the codex **TUI** by `tmux send-keys` (paste + Enter), with banner-poll
readiness. That works, but it's fragile (screen-scraping) and codex's `SessionStart` fires
lazily, so there's no clean pre-input readiness signal. Codex offers a much cleaner surface we
should adopt in a **follow-up** as a *second* agent type (mirroring `mngr_claude`'s
`claude` + `headless_claude`): the **app-server**.

### What it would give us
- **Programmatic messaging instead of tmux paste.** `codex app-server` speaks a JSON-RPC
  protocol over a socket; you send a turn with `initialize` -> `thread/start` -> `turn/start`.
  No `send-keys`, no paste-visibility polling.
- **You can still view it in the TUI.** Launch the TUI as a *viewer* with
  `codex --remote unix://<sock>` (accepts `ws://`, `wss://`, `unix://` too) connected to the
  app-server -- so it runs in tmux and you watch it live, but mngr drives it over the socket.
- **Clean synchronous readiness.** The `initialize` response / `thread.started` event is an
  unambiguous "ready for input" signal -- it eliminates the lazy-`SessionStart` banner-poll
  workaround entirely.
- **Cleaner lifecycle/transcript.** `turn.started` / `turn.completed` / `item.*` events could
  drive the RUNNING/WAITING marker and the transcript directly, instead of (or alongside) the
  hook scripts. The hooks, subagents, sandbox, and approval policy are all **engine-level**
  (`codex-core`), so they fire identically whether codex is driven via the TUI or the
  app-server -- the existing marker hooks would keep working.

### How (verified against codex 0.138.0)
- `codex app-server --listen unix://<sock>` runs the server and **works with the brew/npm
  install**. (The convenience wrapper `codex remote-control start` / `codex app-server daemon`
  requires codex's *standalone* installer at a fixed path -- avoid it; use raw `app-server
  --listen`.) `codex app-server proxy --sock <sock>` proxies stdio to a running server's
  control socket.
- mngr would override `send_message` to speak JSON-RPC to the socket, and `assemble_command`
  would launch `app-server` + a `--remote` TUI viewer instead of the bare TUI.

### Important: client identity and OpenAI's ToS (do NOT spoof the TUI)
The app-server sets its `originator` from the `initialize` request's `clientInfo.name`. It is
**tempting but against the spirit (and likely the letter) of OpenAI's terms** to set
`clientInfo.name = "codex-tui"` so the backend grants the `*-codex` model entitlement it
otherwise denies non-TUI clients. That presents a programmatic client as the first-party TUI
specifically to bypass an *intentional* server-side model gate -- which falls under OpenAI's
"circumvent any restrictions / bypass any protective measures" clause (codex's own code treats
these names as a trust boundary; the override env var is literally `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`).

So the app-server variant must **identify honestly** (mngr's own client name) and use whatever
models that identity is legitimately entitled to. For the gated `*-codex` models in app-server
mode, authenticate with an **API key** (OpenAI's documented path for programmatic workflows) --
do not spoof the TUI. The genuine `codex` TUI agent in this plugin remains the legitimate way
to use `*-codex` models on a ChatGPT-subscription login (it really *is* the TUI).

(Driving codex programmatically via the app-server on a single user's own ChatGPT login is
itself fine -- it's a first-party feature and OpenAI staff have called such use "permissive";
the line we don't cross is identity-spoofing to defeat the model gate, plus the usual no
credential-sharing / no multi-tenant-proxying / no rate-limit-bypass.)
