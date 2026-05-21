# Shared CLAUDE_CONFIG_DIR for claude agents

## Overview

- Today every Claude agent provisions its own per-agent config directory at `$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/`, which mngr populates by symlinking or copying from `~/.claude/`, writing per-agent `.claude.json` / `settings.json` / `installed_plugins.json`, and provisioning per-agent keychain entries on macOS.
- Per-agent isolation is the right default but it has real costs locally: macOS keychain prompts on first run, plugin path rewrites, marketplace re-fetches, sessions split across many `projects/` trees, and ambient writes to `~/.claude.json` for trust/dialog dismissal.
- Some users (especially heavy local-only workflows) prefer the *opposite* trade-off: have every Claude agent share the user's own `CLAUDE_CONFIG_DIR` so credentials, plugins, marketplaces, sessions, and settings just work without copy/symlink shuffling.
- This spec adds a single opt-in flag, `use_env_config_dir: bool = False`, on `ClaudeAgentConfig`. When `True`, mngr does not create or write to a per-agent config dir; the agent inherits the user's `CLAUDE_CONFIG_DIR` from the shell env.
- Strict invariant when the flag is on: **mngr never writes to the user's `CLAUDE_CONFIG_DIR` or to `~/.claude.json`**. Trust, dialog dismissal, credential provisioning, keychain prompts, plugin path rewriting, and per-agent settings.json generation are all skipped. The user is responsible for one-time setup (`claude` interactively at least once: trust dialogs, onboarding, credentials).
- Flag is local-only. Setting it for a non-local host raises a clear error before provisioning. `CLAUDE_CONFIG_DIR` is read from the parent process env; if unset (or empty), mngr falls back to `~/.claude/`, which is claude's own default. So `use_env_config_dir=True` effectively means "don't touch the config dir at all — inherit whatever the parent shell would have used."
- `ORIGINAL_CLAUDE_CONFIG_DIR` is not relevant in this mode and is not set on the agent. `CLAUDE_CONFIG_DIR` is also not explicitly set by mngr on the agent — the agent inherits the parent shell's value (or `~/.claude/` when unset, matching claude's own default).
- Hook scripts in `work_dir/.claude/settings.local.json` and background scripts under `$MNGR_AGENT_STATE_DIR/commands/` are unchanged. Those are per-worktree / per-agent state, not Claude config dir state, so the readiness/permissions/transcript machinery keeps working.
- Other config fields that conceptually overlap with shared-mode behavior (the `sync_*` family, `override_settings_folder`, `settings_overrides`, `convert_macos_credentials`, `auto_dismiss_dialogs`) are simply ignored when `use_env_config_dir=True`. No validation error is raised. The user takes responsibility for the combinations they pick.

## Expected Behavior

### Default (`use_env_config_dir=False`)

- Indistinguishable from today's behavior. Every code path, env var, file layout, and dialog flow is unchanged.

### When `use_env_config_dir=True`

- `mngr create` on a local host succeeds without writing anything inside the directory `CLAUDE_CONFIG_DIR` points to and without writing anything to `~/.claude.json`.
- The launched `claude` process sees `$CLAUDE_CONFIG_DIR` inherited verbatim from the parent shell. Credentials, plugins, marketplaces, skills, agents, commands, keybindings, settings, and prior sessions all come from that one directory.
- Per-agent session JSONLs created by Claude during the run land in `$CLAUDE_CONFIG_DIR/projects/<encoded-work_dir>/` (Claude's normal behavior). Different agents whose `work_dir`s differ get different project subdirs; agents that happen to share a `work_dir` share a project subdir but use distinct `--session-id`s, so their JSONL files do not collide.
- `--adopt-session` keeps working: if the source session already lives in `$CLAUDE_CONFIG_DIR`, mngr finds it there with no copy; if not, mngr still copies the source project dir into `$CLAUDE_CONFIG_DIR/projects/<encoded-work_dir>/`. (Per design decision 4c: this is the only sanctioned addition to the shared dir, and it only adds new project subdirs — it never modifies existing user files.)
- `preserve_sessions_on_destroy` keeps working unchanged: on destroy, session JSONLs, transcripts, and the session-id history are copied to `<local_host_dir>/plugin/mngr_claude/preserved_sessions/<agent>--<id>/` exactly as today. (Sessions live in the user's persistent dir anyway, but preservation also captures the raw + common transcripts and history file from the agent state dir, which the user would otherwise lose.)
- Hooks in `work_dir/.claude/settings.local.json` (readiness, optional auto-allow-permissions, optional macOS credential sync) are written exactly as today. The gitignore preflight check still runs.
- macOS keychain provisioning and cleanup are skipped: Claude Code's keychain label hash uses the shared `CLAUDE_CONFIG_DIR`, which already matches the user's normal `claude` invocations, so no per-agent label exists to populate or delete.
- Trust handling is delegated to the user. mngr does not call `add_claude_trust_for_path`, `auto_dismiss_claude_dialogs`, `acknowledge_cost_threshold`, `dismiss_effort_callout`, `complete_onboarding`, `accept_bypass_permissions`, or `remove_claude_trust_for_path`. If the user has not pre-trusted the work_dir or pre-dismissed onboarding, Claude's TUI will block at startup; this is documented as the user's responsibility.
- The "custom API key" approval dialog (`approve_api_key_for_claude`) is **not** called in shared mode (it writes per-agent .claude.json data we no longer produce). If `ANTHROPIC_API_KEY` is supplied via `--env`/`--pass-env`/host env and does not match the user's `primaryApiKey` or `customApiKeyResponses.approved` list in `~/.claude.json`, Claude will challenge in the TUI and deadlock `wait_for_ready_signal`. This is flagged in **Open Questions** below; the spec recommends a preflight warning but no automatic fix.
- Other config fields are silently ignored when they no longer apply (the `sync_*` family, `override_settings_folder`, `settings_overrides`, `convert_macos_credentials`, `auto_dismiss_dialogs`). `auto_allow_permissions`, `preserve_sessions_on_destroy`, `check_installation`, and `version` continue to work as today since they don't touch the user's config dir.
- The only hard-error check is: host-must-be-local (fires in `on_before_provisioning`, message names the flag). `$CLAUDE_CONFIG_DIR` is read from the env when set, falling back to `~/.claude/` when not — never an error.
- `claude_config.get_claude_config_dir()` (the standalone function) is unchanged: still reads `CLAUDE_CONFIG_DIR` or falls back to `~/.claude` (per decision 1b).
- `ClaudeAgent.get_claude_config_dir()` (the instance method) checks the flag: when `True`, it returns the value of `CLAUDE_CONFIG_DIR` (or `~/.claude/` when unset); when `False`, returns the per-agent path as today.
- `claude_config.get_user_claude_config_dir()` and `find_user_claude_config()` are unchanged. They are not called from shared-mode code paths (callers either branch on the flag or only run in the default mode).

## Implementation Plan

Packaged as a single PR. Branch: `mngr/single-claude-data-dir`.

### `libs/mngr_claude/imbue/mngr_claude/claude_config.py`

- No behavioral change to `get_claude_config_dir`, `get_user_claude_config_dir`, or `find_user_claude_config`. Per decision 1b, the standalone functions keep their existing semantics; only the instance method on `ClaudeAgent` changes.
- Add a new module-level helper:
  ```python
  def resolve_shared_claude_config_dir() -> Path:
      """Return $CLAUDE_CONFIG_DIR, falling back to ``~/.claude/`` when unset."""
  ```
  Reads `$CLAUDE_CONFIG_DIR` from the parent process env when non-empty; otherwise returns `Path.home() / ".claude"`. No errors raised — the fallback matches the directory claude itself picks when the env var is unset, so `use_env_config_dir=True` becomes the "don't touch the config dir" knob even on machines where the user never sets `CLAUDE_CONFIG_DIR`.

### `libs/mngr_claude/imbue/mngr_claude/plugin.py`

#### `ClaudeAgentConfig` (new field)

- Add field:
  ```python
  use_env_config_dir: bool = Field(
      default=False,
      description=(
          "When True, share the user's $CLAUDE_CONFIG_DIR across all claude agents "
          "instead of provisioning a per-agent config dir. Local hosts only. "
          "When set, mngr never writes to the user's Claude config; the user must "
          "have completed `claude` setup interactively (trust, onboarding, credentials)."
      ),
  )
  ```
- No model validator. Other `sync_*` / `override_*` / `settings_overrides` / `auto_dismiss_dialogs` fields are silently ignored at provisioning time when `use_env_config_dir=True` (because the code paths that read them are skipped). Documenting this in the field's description is sufficient.

#### `ClaudeAgent.get_claude_config_dir`

- Branch on `self.agent_config.use_env_config_dir`:
  - `True`: return `resolve_shared_claude_config_dir()`.
  - `False`: return `self._get_agent_dir() / "plugin" / "claude" / "anthropic"` (unchanged).

#### `ClaudeAgent.modify_env_vars`

- Branch on the flag:
  - `True`: do not set `CLAUDE_CONFIG_DIR` or `ORIGINAL_CLAUDE_CONFIG_DIR`.
  - `False`: unchanged.

#### `ClaudeAgent.on_before_provisioning`

- Add a new local-only-check block at the top (before any other logic): if `self.agent_config.use_env_config_dir is True and not host.is_local`, raise `UserInputError("use_env_config_dir is local-only; host is remote")`.
- Re-validate that `$CLAUDE_CONFIG_DIR` is set in the parent process env (calls `resolve_shared_claude_config_dir()` purely for its side-effect of raising). This is fail-fast — the same check happens at field resolution but doing it here surfaces the error inside `on_before_provisioning`'s usual error path.
- Skip the existing dialog-dismissal validation block when the flag is on (the block calls `check_claude_dialogs_dismissed(find_user_claude_config(), trust_path)` — in shared mode we deliberately do not validate dialogs, per decision 2c).
- API-credentials availability warning: keep as-is. (`_has_api_credentials_available` only reads, doesn't write.)

#### `ClaudeAgent.provision`

- Top of method, after `_resolve_plugins_dir_sentinel`: branch on the flag.
- When `use_env_config_dir=True`:
  - Skip `_resolve_plugins_dir_sentinel(host)` (this would rewrite the *user's* `installed_plugins.json` / `known_marketplaces.json`).
  - Skip `interactively_dismiss_claude_dialogs` and `auto_dismiss_claude_dialogs`.
  - Skip `acknowledge_cost_threshold(find_user_claude_config())`.
  - Skip `_setup_per_agent_config_dir`.
  - Keep: background-script provisioning, claude installation check / install, `_transfer_source_plugin_data` (operates on `$MNGR_AGENT_STATE_DIR/plugin/` not the config dir), `_configure_agent_hooks`.
- When `use_env_config_dir=False`: unchanged.

#### `ClaudeAgent.on_after_provisioning`

- The `--adopt-session` block uses `self.get_claude_config_dir()` to find the per-agent `projects/` dir. In shared mode, `get_claude_config_dir()` now returns the user's dir, so `--adopt-session` writes copies of session files into `$CLAUDE_CONFIG_DIR/projects/<encoded-work_dir>/`. No code change required here — the indirection through `get_claude_config_dir()` handles it. This matches decision 4c.

#### `ClaudeAgent.on_destroy`

- The per-agent-config-dir existence check (`test -d <config_dir>`) plus the macOS keychain cleanup remains correct: in shared mode the per-agent dir doesn't exist, so the `if per_agent_config_exists and is_macos()` branch is skipped naturally.
- The "legacy" else-branch currently calls `remove_claude_trust_for_path(find_user_claude_config(), self.work_dir)`. Per decision 3b, **leave this call as-is** — `remove_claude_trust_for_path` already gates on `_mngrCreated=True`, and in shared mode we never set that marker on any user-config entry, so the call is a guaranteed no-op. No behavioral change, no risk, and we get the "legacy agent without per-agent config dir" path for free without further branching.
- Session preservation (`_preserve_session_files`) keeps working unchanged: it calls `agent.get_claude_config_dir()` which now returns the user's dir, so it pulls session JSONLs from there.

#### Deploy path (`get_files_for_deploy`, `modify_env_vars_for_deploy`)

- No change. These run at deploy-image build time, where there is no `ClaudeAgentConfig` instance and no host. The flag is local-only and irrelevant to deployment.

### `libs/mngr_claude/imbue/mngr_claude/headless_claude_agent.py`, `code_guardian_agent.py`, `fixme_fairy_agent.py`, `skill_agent.py`

- No code change. They inherit `ClaudeAgentConfig`'s new field and `ClaudeAgent`'s updated `provision`/`get_claude_config_dir`/`modify_env_vars` automatically.
- One caveat: `HeadlessClaudeAgent` runs `claude -p ...` non-interactively; the same rules apply (user must have pre-trusted + pre-dismissed dialogs). `auto_dismiss_dialogs=True` is silently ignored in shared mode, so headless + shared-mode users must ensure dialogs are already dismissed in `~/.claude.json` themselves.

### Tests

- New unit tests in `libs/mngr_claude/imbue/mngr_claude/claude_config_test.py`:
  - `test_resolve_shared_claude_config_dir_returns_env_value` — set `CLAUDE_CONFIG_DIR`, assert the helper returns its `Path`.
  - `test_resolve_shared_claude_config_dir_falls_back_when_unset` / `…_when_empty` — unset / empty env, assert `Path.home() / ".claude"`.
- New unit tests in `libs/mngr_claude/imbue/mngr_claude/plugin_test.py`:
  - `test_claude_agent_get_claude_config_dir_uses_env_in_shared_mode` — instantiate agent with flag on, monkeypatch env, assert `get_claude_config_dir()` returns the env value.
  - `test_claude_agent_modify_env_vars_omits_claude_config_dir_in_shared_mode` — assert `CLAUDE_CONFIG_DIR` and `ORIGINAL_CLAUDE_CONFIG_DIR` are not in the resulting dict.
- New integration test (offload-only, `test_*.py` style) in `libs/mngr_claude/imbue/mngr_claude/test_shared_config_dir.py`:
  - `test_shared_config_dir_local_agent_does_not_touch_user_config` — create a local agent with `use_env_config_dir=True`, point `CLAUDE_CONFIG_DIR` at a snapshot of a real `~/.claude` (copied to `tmp_path`), capture mtimes of `.claude.json`/`settings.json`/`projects/` before run, start the agent, send a no-op message, destroy, assert mtimes of pre-existing files are unchanged (newly created `projects/<encoded>/...` files are allowed).
  - `test_shared_config_dir_remote_raises` — attempt to create a Modal agent with the flag, assert `UserInputError` from `on_before_provisioning`.
  - `test_shared_config_dir_unset_env_falls_back_to_home` — `monkeypatch.delenv("CLAUDE_CONFIG_DIR")`, create agent, assert `get_claude_config_dir()` resolves to `~/.claude/` and provisioning succeeds.
  - `test_shared_config_dir_adopt_session_writes_under_shared_projects_dir` — `--adopt-session` with an external `.jsonl` file, assert it is copied into `$CLAUDE_CONFIG_DIR/projects/<encoded-work_dir>/`.

### Docs / changelog

- `changelog/mngr-single-claude-data-dir.md` — already created; expand to user-facing language describing the new flag, its purpose, and the user-side prerequisites (one-time interactive `claude` setup).
- `libs/mngr_claude/README.md` — short paragraph naming the flag and pointing at the changelog entry.

## Implementation Phases

Phase order is chosen so each phase ends in a working, releasable state.

### Phase 1 — Config plumbing only

- Add the `use_env_config_dir` field on `ClaudeAgentConfig`.
- Add `resolve_shared_claude_config_dir` in `claude_config.py`.
- Unit tests for the field default and the helper.
- No behavior change yet — agents with the flag set don't yet provision differently. (Will fail at runtime because we haven't updated provision/modify_env_vars/get_claude_config_dir; this is internal-only and won't ship a release boundary until Phase 2.)

### Phase 2 — Wire flag into agent runtime

- Update `ClaudeAgent.get_claude_config_dir` to branch on the flag.
- Update `ClaudeAgent.modify_env_vars` to branch on the flag.
- Update `ClaudeAgent.on_before_provisioning` with the local-only check and skip the dialog-dismissal validation.
- Update `ClaudeAgent.provision` to skip per-agent config setup, dialog handling, and `acknowledge_cost_threshold` when the flag is set.
- Unit tests for env-var omission and config-dir resolution.
- After this phase, local-only shared-config agents work end-to-end.

### Phase 3 — Tests + docs

- Add integration tests in `test_shared_config_dir.py`.
- Update changelog entry with the final user-facing language.
- Update `libs/mngr_claude/README.md` with a short blurb pointing at the flag.

## Testing Strategy

### Unit tests (run via `just test-quick libs/mngr_claude`)

- Cover `resolve_shared_claude_config_dir`: set / unset / empty-string env var.
- Cover `ClaudeAgent.get_claude_config_dir` and `modify_env_vars` directly with a minimally-constructed agent (use existing `temp_mngr_ctx` / `temp_host_dir` fixtures from `libs/mngr/imbue/mngr/utils/testing.py`).

### Integration tests (acceptance/release, run via offload)

- Provision a real local agent with `use_env_config_dir=True` against a `tmp_path`-rooted fake `CLAUDE_CONFIG_DIR` (populated with a minimal but realistic `.claude.json` + `settings.json` + `projects/`). Capture file mtimes / SHA256 hashes before and after agent lifecycle; assert pre-existing files are byte-identical at destroy time.
- Confirm hooks are still written to `work_dir/.claude/settings.local.json`.
- Confirm `$MNGR_AGENT_STATE_DIR/commands/` is populated.
- Confirm background-task transcripts still emit to `events/claude/common_transcript/events.jsonl`.
- Confirm `--adopt-session` writes to `$CLAUDE_CONFIG_DIR/projects/<encoded-work_dir>/`, not to a per-agent dir.

### Edge cases

- `use_env_config_dir=True` + remote host → `on_before_provisioning` raises.
- `use_env_config_dir=True` + `CLAUDE_CONFIG_DIR` unset → silently falls back to `~/.claude/`. No error.
- `use_env_config_dir=True` + work_dir is not trusted in the shared config → Claude's TUI blocks; `wait_for_ready_signal` times out; verify the existing trust-dialog indicator catches it (`TrustDialogIndicator` matches "Yes, I trust this folder"). No code change needed, but add an integration test that confirms the error surface is reasonable.
- `use_env_config_dir=True` + `ANTHROPIC_API_KEY` provided via mngr that doesn't match user's `primaryApiKey` → `CustomApiKeyDialogIndicator` fires; documented in Open Questions.
- Concurrent agents sharing the same `work_dir` → distinct `--session-id`s, files in same `projects/<encoded>/` but no collision. Acceptable; covered by inspection rather than automated test.

### Ratchets

- No new ratchet rules needed. The existing `PREVENT_HARDCODED_CLAUDE_DIR` ratchet keeps callers off of `Path.home() / ".claude"`.

## Open Questions

These are flagged for the user to decide before or during implementation; the spec carries reasonable defaults.

- **API-key mismatch deadlock**: When `use_env_config_dir=True` and a non-empty `ANTHROPIC_API_KEY` is provided via mngr (`--env` / `--pass-env` / `--host-env-file`) that doesn't appear in the user's `primaryApiKey` or `customApiKeyResponses.approved` list in `~/.claude.json`, Claude will challenge in the TUI and `wait_for_ready_signal` will time out. Options: (1) ignore — let the existing dialog indicator surface a clean error; (2) preflight check in `on_before_provisioning` that reads `~/.claude.json` (read-only) and raises if a mismatch is detected; (3) add a separate, narrowly-scoped opt-in that *does* allow writing the `customApiKeyResponses` field of `~/.claude.json` for shared mode. Spec recommends (2) — read-only preflight with a clear error message — but defers to the user.

- **`mngr` writing concurrent to user's own `claude` invocations**: With shared mode, both mngr-launched agents and the user's own `claude` invocations write to `~/.claude.json` (the user's own writes; mngr's writes are eliminated by this flag). The user's `claude` doesn't honor mngr's `_claude_config_lock`, so an interactive `claude` run during `mngr create` could lose updates. Likely acceptable since shared mode is opt-in and the user is explicitly choosing this trade-off; documented for clarity.

- **Plugin-level default**: Currently the flag lives on `ClaudeAgentConfig` (per decision 1a) — to opt all claude agent types into shared mode, the user must set it on each type. Should we *also* expose this as a plugin-level config in mngr's TOML (`[plugin.mngr_claude]`) that becomes the field's default? Out of scope for v1; raise later if a real workflow demands it.

- **`HeadlessClaudeAgentConfig.auto_dismiss_dialogs` default**: Headless mode typically wants `auto_dismiss_dialogs=True`. In shared mode this field is silently ignored, so headless + shared-mode users must ensure dialogs are pre-dismissed in `~/.claude.json`. If we expect that combo to be common, we may want to surface a warning at provisioning time. Out of scope for v1.

- **Flag naming**: `use_env_config_dir` is descriptive but slightly oblique. Alternatives: `share_user_config_dir`, `use_user_claude_config_dir`, `shared_config_dir`. Spec keeps `use_env_config_dir` per the user's original request.
