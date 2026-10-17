Rebuilt the `codex` agent to run entirely over the stock `codex app-server` (JSON-RPC) instead of screen-scraping the TUI, while preserving every mngr harness contract at 1:1 parity with the TUI form. The app-server is invisible to clients: `mngr create` / `message` / `stop` / `start` / `connect` / `destroy` and `mngr ls` lifecycle behave exactly as for claude and pi.

- `CodexAgent` now subclasses `BaseAgent` (like pi/opencode) with its own `send_message` (`turn/start` when idle, `turn/steer` when a turn is running) and a `thread/status`-derived RUNNING/WAITING lifecycle; the old `tmux send-keys` drive and the on-disk marker hooks are gone.

- `mngr create` establishes ONE durable root conversation up front — `thread/start` + `thread/inject_items` materializes the rollout with no model turn — and persists it, so the terminal (`codex resume <id> --remote`), `mngr message`, and Minds all land in the same conversation; `mngr stop`/`start` resumes it.

- The daemon now launches with `--dangerously-bypass-hook-trust --enable hooks`, and create auto-clears codex's one-time "Hooks need review" screen, so the workspace safety hooks (PreToolUse guards) and the transcript recorder actually run — on typed AND web/CLI turns.

- Fixes found by driving the real release lifecycle: the unix-socket path moved to a short `/tmp` location (a deep state dir exceeded `SUN_LEN`, so the daemon could not bind); mngr records the rollout path itself (`codex_transcript_path`) since the hook that used to does not fire before the first turn; the transcript supervisor no longer races the executable-bit on its scripts; and an `--adopt`/`--from` agent gets its send/transcript pointers written so it recalls its resumed history.
