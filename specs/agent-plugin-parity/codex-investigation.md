# Codex CLI investigation (for `mngr_codex`)

Verified findings about the OpenAI Codex CLI (Rust `codex`, **v0.138.0**), mapped to the
[parity dimensions](./spec.md). Source claims are checked against the openai/codex repo at
tag `rust-v0.138.0` (commit `c18e9f4`) and confirmed live where noted. This is the design
basis for the `libs/mngr_codex/` plugin.

**Headline: Codex is the closest CLI yet to Claude Code** -- same hook model, a config-dir
override env var, file-based auth, resume-by-id, an append-as-you-go session JSONL. So
`mngr_claude` is the primary template, with `mngr_antigravity` as the model for the
banner-poll readiness fallback and the single-type plugin skeleton.

## Config & auth

- **Config-dir override**: `CODEX_HOME` env var (default `~/.codex`). The preferred
  isolation shape (cf. `CLAUDE_CONFIG_DIR`); the user's real `$HOME` is untouched, so no
  HOME-relocation collateral. config.toml + `-c key=value` overrides + per-profile
  `$CODEX_HOME/<name>.config.toml` layering.
- **Auth**: `$CODEX_HOME/auth.json` (mode 600), JSON `{auth_mode, OPENAI_API_KEY, tokens,
  last_refresh, agent_identity?, personal_access_token?}`. No OS keychain by default.
  - Written **in place**: `FileAuthStorage::save` opens the same path `O_TRUNC|O_WRONLY|
    O_CREAT` and writes -- no temp-file+rename. So a per-agent `auth.json` **symlink** to a
    shared `~/.codex/auth.json` writes through (like agy's token symlink).
  - Refresh is **multi-instance-aware**: `refresh_token()` reloads the file first and adopts
    a newer on-disk token instead of re-refreshing, so concurrent agents sharing one file
    propagate refreshes without clobbering. (No cross-process flock; rare double-refresh
    window is tolerated.)
  - **Must pin `cli_auth_credentials_store = "file"`** in each agent's config.toml. The
    `keyring`/`auto`/`ephemeral` backends key the secret by `SHA256(canonical CODEX_HOME)`,
    so each home would get a *different* entry and sharing would be impossible. `file` is the
    current default but `auto` exists, so pin it explicitly for cross-platform robustness.
  - `CODEX_API_KEY` env var fully bypasses auth.json (API-key billing, not ChatGPT). Note
    `OPENAI_API_KEY` env does **not** bypass auth.json in this version (only onboarding
    prefill).

## Lifecycle & subagents (the crux)

- **Hooks**: full Claude-style system, feature `hooks` is `stable=true`. Events:
  `SessionStart, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, PreCompact,
  PostCompact, SubagentStart, SubagentStop, Stop, Notification`. Configured via
  `$CODEX_HOME/hooks.json` or `[hooks]` in config.toml; `type:"command"` handlers get the
  event JSON on **stdin**. **Verified firing live** in the TUI: `SessionStart` ->
  `UserPromptSubmit` -> `Stop` in order on a full turn.
- **Marker plan**: `UserPromptSubmit` touches `$MNGR_AGENT_STATE_DIR/active`; `Stop` removes
  it. Hook scripts are POSIX `sh`, parse JSON with `grep`/`sed` (no `jq` on remote hosts).
- **Subagent gating = Claude's model, not agy's.** `Stop` fires **only at root-agent
  scope**. Task-style subagents fire a *distinct* `SubagentStop` (and run in **separate
  rollout files**, linked by `parent_thread_id`). So, like `mngr_claude`, we simply **do not
  hook `SubagentStop`** -- subagents never touch the marker by construction.
- **Nested whole-process guard**: a recursive `codex` subprocess sharing the same
  `CODEX_HOME` would fire its own `SessionStart`/`Stop`. Discriminator: the `SessionStart`
  payload carries `session_id` and `source` (`startup`/`resume`/`clear`/`compact`); record
  the root session id and have `Stop` clear only when its `session_id` matches the recorded
  root (analogous to agy's `root_conversation`, and to claude's `SESSION_GUARD`).
- **Hook input payloads** (verified): all events carry `session_id, transcript_path, cwd,
  hook_event_name, model, permission_mode`; turn events add `turn_id`; `SessionStart` adds
  `source`; `Stop`/`SubagentStop` add `stop_hook_active, last_assistant_message`;
  `UserPromptSubmit` adds `prompt`.
- **Hook trust**: non-managed command hooks must be trusted (by hash) before they run. Two
  options: (a) seed `[hooks.state."<key>"] {enabled=true, trusted_hash="sha256:<hex>"}` in
  the user-layer config.toml -- key is `"<source-path>:<event_label>:<group_idx>:<handler_idx>"`,
  hash is `sha256` of canonical-JSON of the normalized `{event_name, matcher, hooks:[handler]}`
  identity (handler = `{type,command,commandWindows,timeout,async,statusMessage}`); or (b)
  pass `--dangerously-bypass-hook-trust`. Seeding is exact-but-brittle (positional keys, a
  source `TODO` to make them durable); the bypass flag is the robust fallback for our fixed,
  self-authored hooks.

## Readiness

- **No pre-input sentinel.** `SessionStart` fires **lazily** -- on the first user prompt, not
  at TUI launch (confirmed live; openai/codex issue #15269). So, like antigravity, fall back
  to the `InteractiveTuiAgent` banner poll on a stable TUI string (the composer prompt glyph
  `>` / status line), not a hook sentinel.
- **Future alternative (not this PR)**: driving `codex app-server` (JSON-RPC over stdio) gives
  explicit synchronous lifecycle events -- the `initialize` response and `thread/started`
  notification are unambiguous pre-input readiness signals, plus `turn/started`,
  `turn/completed`. This sidesteps the lazy-SessionStart bug but is a fundamentally different
  agent architecture (a persistent JSON-RPC driver, no tmux / no `InteractiveTuiAgent`), out
  of scope here; noted as a cleaner future direction.

## Sessions & transcripts

- **Location**: `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<session-uuid>.jsonl`.
  `transcript_path` in hook payloads points here.
- **Schema** (5 line types, wire form `{timestamp, type, payload}`): `session_meta` (first
  line; `{id, cwd, originator, cli_version, parent_thread_id?, source, thread_source?, ...}`),
  `response_item` (model history -- `message{role,content}`, `reasoning`, `function_call`,
  `function_call_output`, ...), `event_msg` (display duplicates + lifecycle:
  `user_message`, `agent_message`, `task_started`/`task_complete`, `token_count`, ...),
  `turn_context`, `compacted`.
  - **User/assistant text appears twice**: `response_item/message` (model view) **and**
    `event_msg/user_message`|`agent_message` (display view). Pick one to avoid duplicates.
    Tool results are `response_item/function_call_output` only (no `event_msg` dup). Recommended
    common-transcript mapping: `user_message` <- `message{role:user}`, `assistant_message` <-
    `message{role:assistant}`, `tool_result` <- `function_call`+`function_call_output` paired
    by `call_id`.
  - **No global/per-line index or uuid** -- only `timestamp` (ms, not unique), `call_id`
    (tool call<->output), `turn_id`. Dedupe downstream by `(session_uuid, byte_offset)` or
    line number (file is append-only). Parser should tolerate unknown `type`s / missing
    fields (codex uses `#[serde(default)]` + catch-alls).
  - **Subagents** write **separate** rollout files (linked by `parent_thread_id`,
    `thread_source:"subagent"`); the parent file does not embed subagent transcript content.
- **`codex exec --json`** emits a *different*, higher-level `ThreadEvent` schema
  (`thread.started`, `turn.started/completed`, `item.*`) -- a live stream option, but the
  rollout file is the authoritative, resumable record.

## Conversation resume

- `codex resume <SESSION_ID-uuid>` or `codex resume --last`; also `fork`, `archive`.
- **Survives a hard kill** (`mngr stop` SIGKILLs the process -- no flush-on-exit): the
  rollout JSONL is `write_all`+`flush` per line as the turn happens, and resume reconstructs
  history from the file's `response_item`/`compacted` lines (not from the sqlite `state` DB,
  which is a rebuildable metadata index with a filename-scan fallback). So a hard kill at any
  moment leaves a complete, resumable transcript up to the last flushed line. (Caveat: it's a
  userspace flush to the OS, not `fsync`, so a kernel/power crash -- not an ordinary SIGKILL
  -- could lose only the final buffered line.) No `--session-id` pin at fresh start, so
  **capture the root `session_id`** from a hook into a tracking file, then shell-eval `codex
  resume <id>` (falling back to a fresh start) in `assemble_command`.

## Permissions & trust

- **Trust**: the "trust this folder" dialog is **TUI-only**, gated by
  `active_project.trust_level.is_none()`. Seed `[projects."<path>"] trust_level = "trusted"`
  in config.toml. Path lookup tries literal cwd, **canonicalized** cwd (resolves symlinks),
  then the git repo root -- so seed the **canonical** absolute work-dir path. No global
  "trust all". Trust only sets *defaults* (`approval_policy`/`sandbox_mode`), not auto-approve.
  Split durable (repo root) vs transient (per-agent worktree) like agy.
- **Onboarding NUX** (seed for a silent first launch): valid `auth.json` (skips login);
  `personality` set or `$CODEX_HOME/.personality_migration` marker; `[notice]` hide flags
  (`hide_full_access_warning`, `hide_world_writable_warning`, `hide_rate_limit_model_nudge`,
  `hide_<model>_migration_prompt`); `[tui.model_availability_nux]` per-slug counts;
  optional `[tui] show_tooltips=false animations=false`.
- **Auto-approve (unattended)**: `approval_policy = "never"` + `sandbox_mode =
  "workspace-write"` (or `read-only`) suppresses **all** approval dialogs *with the sandbox
  still on* -- the right unattended default. `--dangerously-bypass-approvals-and-sandbox`
  (alias `--yolo`) is the only thing that disables the sandbox. `--full-auto` is removed in
  0.138.0. `codex exec` forces `approval_policy=never` already; the TUI needs it set.
- **Model**: `model` + `model_reasoning_effort` (`none|minimal|low|medium|high|xhigh`) in
  config.toml pin per-`CODEX_HOME`. No per-project model key; `[profiles.<name>]` is the
  alternative override. Don't hardcode -- expose via config and let codex default.

## Misc

- **Process name**: `codex`.
- **Dotted workspace path**: codex accepts `~/.mngr/worktrees/...` as cwd (verified: `codex
  doctor` runs in one). No symlink workaround needed (unlike agy).
- **Install/version**: brew/npm; `codex --version`, `codex doctor`. Installation management
  is optional (lower priority than milestones 1-4).
- **Schedule/deploy**: codex would need deploy file/env contributions to run under `mngr
  schedule` -- a claude-only feature, out of scope for 1-4.

## Out of scope for this PR (milestones 1-4)

Carried-forward gaps, to list in the PR description: session-preservation-on-destroy,
deploy/scheduling contributions, field generators (`waiting_reason` -- codex *does* fire a
`Notification` event and a `PermissionRequest` hook, so a permission-WAITING reason is
*feasible* later, unlike agy), the streaming snapshot, and installation management.

**Future `headless_codex` (noted in PR):** the `codex app-server` JSON-RPC protocol (or `codex
exec --json`) gives clean synchronous lifecycle + stream events (`initialize`/`thread.started`
readiness, `turn.started`/`turn.completed`, `item.*` output). That is the natural basis for a
headless codex subtype -- a `BaseHeadlessAgent`/`StreamingHeadlessAgentMixin` driving
app-server over JSON-RPC -- directly parallel to `mngr_claude`'s `headless_claude` (`claude
--print`, streaming from `stdout.jsonl`). Out of scope here; called out as the recommended
follow-up for a headless variant.
