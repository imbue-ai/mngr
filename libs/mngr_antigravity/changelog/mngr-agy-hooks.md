The `antigravity` agent type now uses agy hooks (re-verified working against agy 1.0.3; an earlier comment claimed hook execution was gated behind a per-account experiment, which is no longer the case).

- mngr provisions a per-agent `hooks.json` and points agy at it with `--add-dir`, so the user's global `~/.gemini/config/` is untouched and each agent's state stays isolated.
- A `PreInvocation`/`Stop` hook pair maintains an `active` marker so antigravity agents now report RUNNING while working and WAITING when idle (previously they had no `active` marker and could not report RUNNING).
- `auto_allow_permissions = true` is now wired through a `PreToolUse` hook returning `{"decision": "allow"}` instead of the `--dangerously-skip-permissions` CLI flag.

Note: the in-TUI `/hooks` command writes to `~/.gemini/antigravity-cli/hooks.json`, which the runtime does not read (agy bug); mngr writes its own per-agent file via `--add-dir` and does not rely on the TUI.
