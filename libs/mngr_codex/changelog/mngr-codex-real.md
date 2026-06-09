Added real `codex` agent-type support as its own plugin (`imbue-mngr-codex`), wiring OpenAI's Codex CLI into mngr and replacing the previous in-core `BaseAgent` stub.

- Per-agent `CODEX_HOME` isolation gives each agent its own config, sessions, and transcripts without relocating the user's real `$HOME`.
- Shared auth via a write-through `auth.json` symlink to a shared `~/.codex/auth.json` (with `cli_auth_credentials_store = "file"` pinned), so logging in once authenticates every agent and token refreshes propagate.
- RUNNING/WAITING lifecycle from a `UserPromptSubmit`/`Stop` hook pair, with subagent-aware gating: subagents fire a distinct `SubagentStop` in separate rollout files and never touch the `active` marker, and `Stop` clears the marker only at root-agent scope (matched against the recorded root `session_id`).
- Conversation resume across stop/start: the root `session_id` is captured into a tracking file and `mngr start` shell-evaluates `codex resume <id>` (falling back to a fresh start). The rollout JSONL is flushed per line, so it survives `mngr stop`'s hard kill.
- Common transcripts readable by `mngr transcript`, plus seeded trust and onboarding for a silent first launch.

Not yet implemented (carried-forward gaps): session-preservation-on-destroy, deploy/scheduling contributions, field generators (`waiting_reason`), the streaming snapshot, and install/version management. A future `headless_codex` subtype driving `codex app-server` over JSON-RPC (clean synchronous lifecycle and stream events) is the recommended follow-up for a headless variant.
