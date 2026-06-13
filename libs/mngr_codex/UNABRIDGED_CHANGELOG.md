# Unabridged Changelog - mngr_codex

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_codex/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-12

Added real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub.

- Per-agent `CODEX_HOME` isolation gives each agent its own config, sessions, and transcripts without relocating the user's real `$HOME`.
- Shared auth via a write-through `auth.json` symlink to a shared `~/.codex/auth.json` (with `cli_auth_credentials_store = "file"` pinned), so logging in once authenticates every agent and token refreshes propagate.
- RUNNING/WAITING lifecycle with subagent-aware gating across four hooks (`UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`). Because codex subagents run asynchronously (the root's `Stop` fires while subagents are still working, with no ordering guarantee on the later `SubagentStop` hooks and no `fullyIdle` signal), the `active` marker is recomputed under a lock from a root-turn flag plus one file per in-flight subagent, so it stays RUNNING until the root turn AND every subagent are done. The `Stop` clear is still guarded against a nested/recursive codex via the recorded root `session_id`.
- Conversation resume across stop/start: the root `session_id` is captured into a tracking file and `mngr start` shell-evaluates `codex resume <id>` (falling back to a fresh start). The rollout JSONL is flushed per line, so it survives `mngr stop`'s hard kill.
- Common transcripts readable by `mngr transcript`, plus seeded trust and onboarding for a silent first launch.
- `send_message` waits for submission to register: the `UserPromptSubmit` hook signals a `mngr-submit-<session>` tmux wait-for channel after it sets the `active` marker, and the sender blocks on that channel, so `mngr message` returns only once the agent reads RUNNING (closes a race where a follow-up lifecycle check could see the pre-turn idle state).
- Update handling: codex's blocking startup "Update available!" prompt is disabled (it would intercept the first message), and mngr surfaces updates itself at provision instead. It compares `codex --version` to the latest version codex recorded in `~/.codex/version.json` (no network call); this check always runs and is best-effort (failures never block provisioning). When codex is outdated, the action is governed by a single `update_policy` setting (default `ASK`): `AUTO` runs `codex update` with no prompt, `ASK` prompts on an attended local run (interactive tty + local host, not `--yes`) and otherwise logs a non-blocking notice, and `NEVER` only logs the notice. Because `ASK` is gated on the host being local as well as interactive (mirroring the claude plugin's `is_unattended = not host.is_local`), an unattended remote/deploy agent provisioned from a local terminal defaults to neither prompting nor upgrading the remote's global install.

Not yet implemented (carried-forward gaps): session-preservation-on-destroy, deploy/scheduling contributions, field generators (`waiting_reason`), the streaming snapshot, and install/version management. A future app-server-backed agent variant (drive `codex app-server` over JSON-RPC for programmatic messaging + a `codex --remote` TUI viewer + clean `initialize`-based readiness) is the recommended follow-up; its design and the OpenAI-ToS caveat (identify honestly, no `codex-tui` spoofing) are documented in the plugin README.

Added a conformance test asserting that codex's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`). The release test now runs on the
shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`). The
full lifecycle (including stop/start resume) passes end-to-end against the real codex
binary. Simplified the release test's plumbing to reuse the shared `init_git_repo` helper
and the autouse fixture's tmux-server isolation instead of hand-rolling its own git repo
setup and private tmux server. Now that codex's `send_message` blocks until the agent
reads RUNNING (the submit/lifecycle race fix), the release test also observes the RUNNING
marker like the pi and opencode tests do.

## 2026-06-09

Added real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub.

- Per-agent `CODEX_HOME` isolation: each agent runs `codex` under its own `CODEX_HOME` so its config, sessions, and transcripts stay isolated, without relocating the user's real `$HOME`.
- Shared auth: each agent's `auth.json` is a write-through symlink to a shared `~/.codex/auth.json`, so the first agent's login authenticates every other agent and token refreshes propagate ("log in once, anywhere"). `cli_auth_credentials_store = "file"` is pinned so the shared file backend is used.
- RUNNING/WAITING lifecycle with subagent-aware gating: a `UserPromptSubmit`/`Stop` hook pair maintains an `active` marker driving `BaseAgent`'s RUNNING/WAITING detection. Subagents fire a distinct `SubagentStop` and run in separate rollout files, so they never touch the marker by construction; the root session id is recorded so `Stop` clears the marker only at root-agent scope.
- Conversation resume: the root `session_id` is captured from a hook into a tracking file, and `mngr start` shell-evaluates `codex resume <id>` (falling back to a fresh start), so the agent keeps its context across a stop/start. The rollout JSONL is flushed per line, so it survives the hard process kill `mngr stop` performs.
- Transcripts: codex agents emit a common transcript readable by `mngr transcript`, mapping codex's rollout `message`/`function_call`/`function_call_output` lines into the agent-agnostic format.
- Trust and onboarding: the agent's canonical work-dir path is seeded as `trusted` and the onboarding NUX is seeded for a silent first launch, so codex starts without interactive trust/login prompts.
