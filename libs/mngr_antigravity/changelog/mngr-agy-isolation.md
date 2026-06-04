Each `antigravity` agent now runs `agy` under its own per-agent `$HOME` (at `<agent_state_dir>/plugin/antigravity/home/`), giving each agent its own permission policy, model, and isolated config/transcript/session state instead of today's all-or-nothing `--dangerously-skip-permissions` and shared global `~/.gemini`. Two new agent-type config fields:

- `settings_overrides` (dict, default `{}`) -- a free-form blob merged last into the per-agent `settings.json`, covering `permissions` (`{allow, deny, ask}`, precedence Deny > Ask > Allow), `toolPermission`, and `model` (an `agy models` display name). Mirrors `mngr_claude`'s field of the same name.
- `sync_home_settings` (bool, default `true`) -- base the per-agent `settings.json` on a copy of the user's real settings (so agents inherit the user's preferences), with `settings_overrides` layered on top; `false` starts from an empty base.
- `symlink_oauth_token` (bool, default `true`) -- symlink (so refreshes propagate) vs copy the shared oauth token into each agent's home.

Other changes:

- Trust now splits by what is persisted: the durable source-repo path goes to the user's global settings (so trust isn't re-prompted across agents/worktrees of the same repo), while the transient per-agent workspace path goes only into the per-agent settings. Consent gating is unchanged in spirit (interactive prompt / `--yes` / `auto_dismiss_dialogs`, else clean `SystemExit`); mngr never silently runs an agent on untrusted code.
- Lifecycle hooks now live at the per-agent `$HOME/.gemini/config/hooks.json` and execute directly -- the previous `--add-dir` + `/tmp` hooks-symlink workaround is removed.
- agy's first-run NUX is skipped via a seeded `cache/onboarding.json`. If a shared `antigravity-oauth-token` exists at the host user's real `~/.gemini/antigravity-cli/`, it is symlinked/copied into each agent's home so the agent is authenticated without its own login flow; if it does not, provisioning still succeeds and the agent runs agy's normal login on first launch (matching `mngr_claude`, which skips credential seeding rather than blocking agent creation).
- Path resolution is host-aware (the user's real `$HOME` and OS are resolved on the host), so the token/settings/cache sharing works on remote hosts too. Heavy `ms-playwright-go` browser binaries are shared across agents by symlinking each agent's home cache to the user's real host cache.

Auth note: on Linux (mngr's runtime, no keychain) a normal `agy` login writes the shared file token, so auth is shared across agents deterministically. On macOS `agy` uses the login keychain (shared across processes, not per-`$HOME`), so per-agent agents often authenticate automatically after one login (occasionally prompting on first launch). See the package README.
