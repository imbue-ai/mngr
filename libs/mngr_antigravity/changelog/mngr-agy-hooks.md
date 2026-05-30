The `antigravity` agent type now uses agy hooks to report lifecycle state (verified working against agy 1.0.3).

- mngr provisions a per-agent `hooks.json` and points agy at it with `--add-dir` (via a `/tmp` symlink, since agy rejects the dotted state-dir path), so the user's global `~/.gemini/config/` is untouched and each agent's state stays isolated.
- A `PreInvocation`/`Stop` hook pair maintains an `active` marker so antigravity agents now report RUNNING while working and WAITING when idle (previously they had no `active` marker and could not report RUNNING).
- `auto_allow_permissions = true` continues to use the `--dangerously-skip-permissions` CLI flag. agy's documented `PreToolUse` `{"decision": "allow"}` hook output does not actually gate the `run_command` confirmation dialog, so a hook can't replace the flag.

Note: the in-TUI `/hooks` command writes to `~/.gemini/antigravity-cli/hooks.json`, which the hook execution engine never runs (it executes hooks only from `~/.gemini/config/hooks.json` and workspace `.agents/`; the TUI path is loaded for display only -- agy bug, reported as antigravity-cli#49). mngr writes its own per-agent file via `--add-dir` and does not rely on the TUI.
