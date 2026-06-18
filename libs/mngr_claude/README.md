# imbue-mngr-claude

Claude agent type plugin for [mngr](../../README.md).

Provides the `claude`, `code-guardian`, and `fixme-fairy` agent types.

## Shared `CLAUDE_CONFIG_DIR` (local agents)

By default, every Claude agent gets its own per-agent config directory under
`$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/` (populated by copying/symlinking
from `~/.claude/`). Set `use_env_config_dir = true` on the agent type config to
have local Claude agents share the user's `$CLAUDE_CONFIG_DIR` instead:

```toml
[agent_types.claude]
use_env_config_dir = true
```

When enabled:

- `$CLAUDE_CONFIG_DIR` **must** be set in the parent shell; mngr errors out otherwise.
- Only local hosts are supported.
- mngr never writes to the user's Claude config (no trust additions, no dialog
  dismissal, no per-agent settings.json, no keychain provisioning). The user is
  responsible for one-time interactive `claude` setup (trust the work_dir,
  complete onboarding, log in).
- The repo-settings sync and auto-dismiss fields (`sync_repo_settings`,
  `override_settings_folder`, `auto_dismiss_dialogs`) are silently ignored since
  shared mode has no per-agent dir to write into.
- Reduced settings support (a scoped, documented limitation of this mode). In
  normal mode mngr bakes its hooks and `settings_overrides` into the per-agent
  config-dir `settings.json`, and a user `--settings` passes through so Claude
  layers it natively. With no per-agent config dir here, instead:
  - mngr writes its hooks **and** the resolved `settings_overrides` patch into the
    managed `claude --settings` file, which Claude layers (highest precedence) over
    the user's shared config. The fold's base is mngr's hooks, not the shared config
    (Claude layers that itself), so narrowing here only guards against an override
    dropping mngr's hooks.
  - a user-supplied `--settings` (in `cli_args`/`agent_args`) is **rejected** at
    provision: mngr already uses `--settings` here and can't reliably merge a second
    one (its value may be inline JSON, not a file). Put those settings in
    `settings_overrides`, or set `use_env_config_dir=False`.

## Merge intent in `settings_overrides` (`__mngr_merge`)

`settings_overrides` is folded onto a base and lands in the per-agent
`settings.json` that Claude itself reads, so it cannot use mngr's internal
`key__extend`/`key__assign` suffixes — Claude would treat `permissions__extend`
as a junk literal key. (mngr's *own* config, everything outside
`settings_overrides`, still uses those suffixes.) Instead, declare merge intent in
a single top-level `__mngr_merge` map of dotted key path -> operator, which vanilla
Claude silently ignores. A bare key **assigns**, guarded so it errors rather than
silently dropping a non-empty list/dict/set from the base (the error prints the
exact `__mngr_merge` patch to add); `"extend"` **merges** onto the base (list
concat / set union / recursive dict merge), and `"assign"` replaces without the
narrowing guard. Raw `__extend`/`__assign` suffix keys under `settings_overrides`
are a hard error pointing here.

```toml
[agent_types.claude.settings_overrides.permissions]
allow = ["Bash(npm *)"]
[agent_types.claude.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"   # or "assign"
```

## Version pinning and auto-updates

Pin the Claude Code version that gets installed, and control its background
auto-updater:

```toml
[agent_types.claude]
version = "2.1.50"      # install this exact version; provisioning verifies it
update_policy = "NEVER" # disable claude's auto-updater so the pin sticks
```

- `version` — pin the Claude Code version to install (the official installer is run
  with `bash -s <version>`); provisioning verifies the installed binary matches and
  errors on a mismatch. Default: unset (latest).
- `update_policy` — govern Claude Code's background auto-updater: `NEVER` sets
  `DISABLE_AUTOUPDATER=1` in the agent environment so the installed binary stays put,
  `AUTO` leaves the auto-updater enabled, and `ASK` behaves like `AUTO` (claude has no
  interactive update flow). When unset (the default), it resolves to `NEVER`
  (auto-update disabled) so a managed agent stays on its installed version — set
  `AUTO` to opt back into Claude Code's auto-updater. Ignored in `use_env_config_dir`
  (shared) mode, where mngr leaves your claude environment alone. (Note: before this,
  mngr did *not* disable claude's auto-updater on local agents — it inherited your
  `~/.claude.json` `autoUpdates` value, so local agents typically auto-updated.)

## Approximate response streaming (`streaming_snapshot_interval_seconds`)

Set `streaming_snapshot_interval_seconds` on the agent type config to get an
*approximate*, live view of Claude's in-progress assistant text:

```toml
[agent_types.claude]
streaming_snapshot_interval_seconds = 0.25
```

When the value is `> 0`, a background watcher periodically captures the agent's
tmux pane, reverse-maps the rendered assistant text back into markdown, and
writes it to `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer`. When the value
is `<= 0` (the default), the watcher is neither provisioned nor run.

`stream_buffer` format:

- Line 1: the id (`uuid`) of the last *complete* assistant message (empty string
  if none yet), so consumers can tell a genuinely-new streaming message apart
  from leftover text right after a message finished.
- Lines 2+: the in-progress assistant text, reverse-mapped to markdown.

Notes:

- This is best-effort and approximate. It reconstructs bold/italic, inline code,
  links, blockquotes, lists, code blocks, and tables from the terminal rendering;
  heading levels and code-block languages are not recoverable.
- The body is strict-append within a message (snapshots are overlap-stitched) and
  is emptied when the agent goes idle. A table is held back until it stops
  changing across polls, so it appears once rendered rather than row-by-row.
- The watcher is a Python script; the agent host must have `python3` available
  (provisioning fails fast if it does not).
