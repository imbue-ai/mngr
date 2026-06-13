# Unabridged Changelog - mngr_codex

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_codex/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-09

Added real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub.

- Per-agent `CODEX_HOME` isolation: each agent runs `codex` under its own `CODEX_HOME` so its config, sessions, and transcripts stay isolated, without relocating the user's real `$HOME`.
- Shared auth: each agent's `auth.json` is a write-through symlink to a shared `~/.codex/auth.json`, so the first agent's login authenticates every other agent and token refreshes propagate ("log in once, anywhere"). `cli_auth_credentials_store = "file"` is pinned so the shared file backend is used.
- RUNNING/WAITING lifecycle with subagent-aware gating: a `UserPromptSubmit`/`Stop` hook pair maintains an `active` marker driving `BaseAgent`'s RUNNING/WAITING detection. Subagents fire a distinct `SubagentStop` and run in separate rollout files, so they never touch the marker by construction; the root session id is recorded so `Stop` clears the marker only at root-agent scope.
- Conversation resume: the root `session_id` is captured from a hook into a tracking file, and `mngr start` shell-evaluates `codex resume <id>` (falling back to a fresh start), so the agent keeps its context across a stop/start. The rollout JSONL is flushed per line, so it survives the hard process kill `mngr stop` performs.
- Transcripts: codex agents emit a common transcript readable by `mngr transcript`, mapping codex's rollout `message`/`function_call`/`function_call_output` lines into the agent-agnostic format.
- Trust and onboarding: the agent's canonical work-dir path is seeded as `trusted` and the onboarding NUX is seeded for a silent first launch, so codex starts without interactive trust/login prompts.
