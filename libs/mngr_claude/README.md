# imbue-mngr-claude

Claude agent type plugin for [mngr](https://github.com/imbue-ai/mngr).

Provides the `claude`, `code-guardian`, and `fixme-fairy` agent types.

## Config dir isolation (`isolate_local_config_dir`)

By default (`isolate_local_config_dir = true`), every Claude agent gets its own
per-agent config directory under `$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/`
(populated by copying/symlinking from `~/.claude/`), so mngr never has to touch
your default Claude config. Set `isolate_local_config_dir = false` on the agent
type config to have local Claude agents share the user's `$CLAUDE_CONFIG_DIR`
instead:

```toml
[agent_types.claude]
isolate_local_config_dir = false
```

When isolation is disabled (shared mode):

- Only affects local hosts. A non-local agent always uses an isolated config dir
  (the user's config and keychain live on the local machine), so the flag is
  ignored for remote agents rather than rejected.
- `CLAUDE_CONFIG_DIR` resolves to the user's shared dir (`$CLAUDE_CONFIG_DIR`, or
  `~/.claude` when unset) and is injected into the agent environment so claude
  reads the user's real config.
- mngr still writes to the user's Claude config to dismiss the cosmetic startup
  dialogs (trust the work_dir, onboarding, effort callout, cost threshold) so they
  don't intercept automated input -- prompting interactively, or silently when
  `auto_dismiss_dialogs` is set. It writes these into the file claude actually
  reads (`$CLAUDE_CONFIG_DIR/.claude.json`, or `~/.claude.json` when unset). It
  does **not** accept bypass-permissions mode there (that is governed by
  settings.json), and it does no per-agent settings.json or keychain provisioning.
  The user is still responsible for logging in (credentials).
- The repo-settings sync fields (`sync_repo_settings`, `override_settings_folder`)
  are silently ignored since shared mode has no per-agent dir to write into.
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
    `settings_overrides`, or set `isolate_local_config_dir=True`.

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

**macOS subscription users:** keep isolation **off**. Claude Code hashes
`CLAUDE_CONFIG_DIR` into its macOS keychain label, so an isolated agent gets a
separate copy of your credentials that goes stale as claude.ai subscription
tokens refresh, leading to auth failures. Sharing the config dir reuses the same
keychain entry as your own claude. mngr warns about this at agent-creation time
when it detects subscription credentials with isolation enabled.

**Deprecated `use_env_config_dir`:** this is the old name for the inverse of
`isolate_local_config_dir` (`use_env_config_dir = true` == `isolate_local_config_dir
= false`). It is still accepted for backward compatibility but emits a deprecation
warning; prefer `isolate_local_config_dir`. Setting both to contradictory
(non-inverse) values is an error.

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
  `AUTO` to opt back into Claude Code's auto-updater. Ignored when
  `isolate_local_config_dir = false` (shared) mode, where mngr leaves your claude
  environment alone. (Note: before this,
  mngr did *not* disable claude's auto-updater on local agents — it inherited your
  `~/.claude.json` `autoUpdates` value, so local agents typically auto-updated.)

## Approximate response streaming (`streaming_snapshot_interval_seconds`)

Set `streaming_snapshot_interval_seconds` to get an approximate, live view of
Claude's in-progress assistant text:

```toml
[agent_types.claude]
streaming_snapshot_interval_seconds = 0.25
```

When the value is `> 0`, a background watcher periodically writes the in-progress
assistant text to a stream buffer. When `<= 0` (the default), it does not run.

Limitations:

- The snapshot is best-effort and approximate; heading levels and code-block
  languages are not recoverable.
- The agent host must have `python3` available (provisioning fails fast otherwise).
