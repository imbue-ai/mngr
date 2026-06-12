# Claude settings overrides via mngr's assign/extend/narrowing semantics

Status: **spec only -- not yet implemented.** Extends the `mngr/claude-hook-leak` PR
(which fixed the `--settings` collision; see "Background"). Builds directly on
[env-settings-overrides](./env-settings-overrides/concise.md), reusing its
`resolve_extends`, narrowing guard, and `parse_scalar_value`.

Audience: developers implementing the change in `libs/mngr_claude` (and a small reuse
of `libs/mngr` config helpers).

## Overview

- Route user-provided Claude settings through mngr's **existing** config-merge model --
  assign-by-default, opt-in `__extend`, and the narrowing guard -- instead of the
  special-purpose `deep_merge_settings` (hook concatenation) added by the collision fix.
- Stay **schema-free**: `ClaudeAgentConfig.settings_overrides` remains `dict[str, Any]`.
  mngr never enumerates or validates Claude's `settings.json` fields -- the same
  treatment it already gives the open-ended `commands.<cmd>.defaults: dict[str, Any]`
  (the merge machinery stops at the open dict and walks its contents structurally).
- Let the user **control merge recursion depth via TOML nesting**: a nested TOML table
  recurses (sibling-preserving); a JSON-blob string leaf is parsed and treated as a
  **collection** value subject to the narrowing guard -- not an opaque scalar.
- Move `settings_overrides`' write target from the per-agent **config-dir
  `settings.json`** to the **managed `--settings` file**, so it works in
  `use_env_config_dir` mode and merges against mngr's own hooks.
- Treat the raw `--settings` flag (in `cli_args` / `agent_args`) as a single one-level
  `settings_overrides`-style override under the same rules.

## Background

What exists today, and what this builds on:

- **mngr config merge** (`libs/mngr/imbue/mngr/config/key_resolver.py`,
  `config/data_types.py`): bare key = **assign**; `key__extend` = **extend** (list/tuple
  concat, set union, **shallow** dict key-merge -- leaf-level only, no recursion into
  nested aggregates). The **narrowing guard** (`detect_settings_narrowing` /
  `would_assignment_narrow`) is a **hard error** when an assign would drop at least one
  entry from a non-empty base aggregate, unless `allow_settings_key_assignment_narrowing`
  is set. `__extend` results (supersets), no-ops, scalars, and `StringDerivedTuple` are
  exempt. See [env-settings-overrides](./env-settings-overrides/concise.md).
- **Deferred `__extend` precedent**: `create_templates.<name>` options keep their
  `__extend` suffix through config-load when the base lookup is `None`
  (`resolve_extends` + `_is_create_template_option_path`), so they resolve lazily at
  `mngr create` time against the runtime command's params rather than against config
  layers.
- **`ClaudeAgentConfig.settings_overrides: dict[str, Any]`** today: shallow `dict.update`
  into the per-agent config-dir `settings.json` (`_build_settings_json`), at provision.
- **The collision fix (this PR)**: mngr's hooks moved out of the project's
  `settings.local.json` into a private per-agent managed `--settings` file
  (`$MNGR_AGENT_STATE_DIR/plugin/claude/mngr_managed_settings.json`). A user `--settings`
  from `cli_args`/`agent_args` is currently deep-merged (hook-concat) into that file via
  `deep_merge_settings` and stripped from Claude's argv. Claude honors only the **last**
  `--settings` on its command line (verified empirically), which is why the user value
  must be folded into mngr's file rather than passed as a second flag.

## Expected behavior

### The merge model (uniform with mngr config)

- **Base** = mngr's runtime-built managed settings (always-on readiness hooks, plus the
  optional credential-sync and permission-auto-allow hooks).
- **Override** = `settings_overrides` (and any raw `--settings` blob), a schema-free
  nested dict.
- A nested **TOML table** in the override recurses, preserving the base's sibling keys at
  that level. A **leaf** value (scalar, or a JSON-blob string) is parsed with
  `parse_scalar_value` (JSON first, raw-string fallback).
- A **bare leaf** = **assign**: it replaces the base value. Assigning over a **non-empty
  base collection** is **narrowing** -> hard error naming the dotted path, unless
  `allow_settings_key_assignment_narrowing`. Scalars and empty/absent bases never narrow.
- A **`key__extend` leaf** = **extend** the base value (concat / union / shallow
  dict-merge), via `resolve_extends`. Extends are narrowing-exempt (supersets).

This is exactly the model from [env-settings-overrides](./env-settings-overrides/concise.md),
applied to an open `dict[str, Any]` field, with the **base being mngr's runtime hooks**
rather than a lower-precedence config layer.

### Deferred resolution against the runtime base (the crux)

mngr's hooks are **built at provision time**, not present as a config layer at config-load.
So `settings_overrides`' `__extend` keys cannot resolve at config-load against config
layers -- a `hooks.SessionStart__extend` is meant to extend **mngr's runtime readiness
hooks**, which don't exist yet during config loading.

Therefore:

- `settings_overrides`' `__extend` keys are **deferred**: `resolve_extends` preserves them
  through config-load (following the `create_templates` precedent), and mngr resolves them
  at **provision time** via `resolve_extends(managed_base, settings_overrides)`.
- The resolved override (now marker-free) is applied with a new `deep_assign(managed_base,
  resolved)` -- recurse into dicts to preserve siblings, override leaf/list/scalar replaces
  the base leaf. The `__extend` keys already incorporated the base value (concat), so the
  subsequent assign does not double-count.
- After applying, run the narrowing check (`would_assignment_narrow` per resolved leaf,
  or `detect_settings_narrowing` over the whole override) against `managed_base`; on any
  violation, raise the standard narrowing error unless the escape hatch is set.

**Note:** because resolution is deferred to provision, the base for `__extend` and for
narrowing is **mngr's built hooks**, which is precisely what makes "extend mngr's
SessionStart" work and "silently replace mngr's SessionStart" fail loudly.

### Target file

- `settings_overrides` is now applied to the **managed `--settings` file** inside
  `_configure_agent_hooks`, not to the config-dir `settings.json`.
- It works in `use_env_config_dir` mode (the managed file lives in the agent state dir,
  always per-agent), unlike the config-dir `settings.json` path which is skipped there.
- Remove `data.update(config.settings_overrides)` from `_build_settings_json`.
- **Precedence change:** user overrides move from the config-dir `settings.json` layer to
  the `--settings` layer of Claude's settings hierarchy. Document in the changelog.

### Raw `--settings` flag

- Still extracted from `cli_args` / `agent_args` (`partition_settings_args`), unquoted
  (`_unquote_cli_settings_value` for `cli_args`), resolved (inline JSON or a file path read
  via the host), and **stripped from Claude's argv** so only mngr's combined `--settings`
  reaches Claude.
- Its resolved value is applied as a **one-level override** under the same rules:
  assign-by-default with the narrowing guard. A raw blob that drops mngr's `hooks` (or any
  base collection) -> narrowing error directing the user to `settings_overrides` + `__extend`.
- `deep_merge_settings` (the collision fix's hook-concat) is **removed**. The raw flag no
  longer silently concatenates hooks; the additive path is `__extend`. (This changes the
  interim PR behavior, but that behavior was never released.)

### Worked examples

Reviewer's case -- add a `SessionStart` hook without dropping mngr's readiness hooks:

```toml
[agent_types.coder.settings_overrides.hooks]
SessionStart__extend = '[{"hooks": [{"type": "command", "command": "..."}]}]'
```
-> concatenates onto mngr's readiness `SessionStart` hooks. Both fire.

```toml
[agent_types.coder.settings_overrides]
model = "opus"        # scalar assign, no narrowing
```

```toml
[agent_types.coder.settings_overrides.hooks]
SessionStart = '[{"hooks": [{"type": "command", "command": "..."}]}]'
```
-> assign replaces mngr's `SessionStart` list (drops readiness) -> **narrowing error**.
Fix by using `SessionStart__extend`, or set `allow_settings_key_assignment_narrowing`.

Raw flag full-replace -> **narrowing error** (drops mngr's `hooks`):
```
mngr create coder -- --settings '{"hooks": {"SessionStart": [...]}}'
```

### Schema-free guarantee

No code path enumerates Claude's settings keys. `resolve_extends`,
`would_assignment_narrow`/`detect_settings_narrowing`, `deep_assign`, and
`parse_scalar_value` operate structurally over `Mapping` / `list` / `set`. pydantic does
not validate the inner content of `settings_overrides` (a typo in a Claude key is **not**
caught) -- the intended trade-off of schema-free passthrough.

## Changes

`libs/mngr_claude`:

- `ClaudeAgentConfig.settings_overrides`: update the field description -- assign + `__extend`
  with narrowing; written to the managed `--settings` file; examples updated.
- `_configure_agent_hooks`: after building mngr's base hook settings, apply
  `settings_overrides` and the resolved raw `--settings` blob(s) via
  `resolve_extends(base, override)` -> `deep_assign` -> `would_assignment_narrow` guard.
  Replace the `deep_merge_settings` call.
- `_build_settings_json`: drop `data.update(config.settings_overrides)` (no longer targets
  config-dir `settings.json`); keep `compute_settings_json_flags`.
- Raw `--settings`: keep `partition_settings_args` + argv strip +
  `_unquote_cli_settings_value` + inline-JSON/file-path resolver; route through the same
  apply pipeline as `settings_overrides`.

`libs/mngr_claude/imbue/mngr_claude/claude_config.py`:

- Add `deep_assign(base, override)` (recurse dicts, override leaf/list/scalar replaces,
  siblings preserved). **Remove** `deep_merge_settings` and its tests.
- Keep `partition_settings_args`, `_unquote_cli_settings_value`, and the blob resolver.

`libs/mngr` (reuse, minimal change):

- Reuse `resolve_extends`, `would_assignment_narrow` / `detect_settings_narrowing`,
  `parse_scalar_value`.
- Generalize the deferred-`__extend` carveout so `settings_overrides`' `__extend` keys are
  preserved through config-load for provision-time resolution (see Open Questions for the
  exact mechanism).

Docs / changelog: update the mngr_claude README/settings docs; add changelog entries for
both packages; include a **breaking-change** migration note (target move + assign-with-
narrowing for `settings_overrides`).

## Open questions

1. **Cross-layer `settings_overrides.__extend`.** How do `x__extend` keys from multiple
   config layers (user / project / local / env / `--setting`) combine *before* the
   provision-time resolution against mngr's base? Options: (a) stack them additively across
   layers as raw `__extend` ops, resolve once at provision; (b) resolve cross-layer extends
   at config-load against each other, defer only the final extend against mngr's base.
   Prefer (a) for a single, predictable resolution point. Needs nailing down.
2. **Deferred-resolution mechanism.** Extend `resolve_extends`' create-template carveout
   into a generic "deferred paths" set that includes `settings_overrides` (under any
   `agent_types.<name>` / plugin path), or add a dedicated preserve-path. Prefer a small
   generic mechanism so the two deferred cases share code.
3. **Narrowing escape-hatch granularity.** `allow_settings_key_assignment_narrowing` is
   global. Is a per-agent-type opt-out wanted for an intentional full-replace, or is the
   global flag sufficient? (Default: reuse the global flag.)
4. **File-path `--settings` values on remote hosts.** Confirm read-via-host error
   semantics (`UserInputError`) and that the narrowing guard applies to the file's parsed
   content the same as inline JSON.
5. **`sync_home_settings` interaction.** `_build_settings_json` still seeds the config-dir
   `settings.json` from the user's `~/.claude/settings.json`. Confirm that moving
   `settings_overrides` off that path does not change which file "wins" for keys the user
   also sets in their home settings (Claude merges the two layers; document precedence).
6. **`settings_overrides` vs raw `--settings` ordering.** When both are present, define
   which applies first (and thus what the other narrows against). Proposal: apply
   `settings_overrides` first, then the raw flag, so an explicit per-invocation flag layers
   on top -- but both are guarded against mngr's base, so the more conservative option is to
   forbid combining them (error). Decide.

## Tests

- `deep_assign` unit: recurse, replace leaves/lists, preserve siblings, no mutation.
- `resolve_extends` applied to Claude-settings dicts: `hooks.SessionStart__extend` concat
  onto a built base; `model` scalar assign; nested-table recursion preserving base events.
- Narrowing: hard error on `hooks`/`hooks.SessionStart` assign that drops mngr entries;
  passes on `__extend` superset and on pure additions; scalar assign exempt; escape hatch
  bypasses.
- Raw `--settings` blob: full-replace -> narrowing error; additive (`__extend` or
  non-overlapping keys) -> ok; file-path value resolved and guarded.
- `use_env_config_dir`: `settings_overrides` reaches the managed file (and nothing is
  written to the user's real config dir).
- Reviewer's exact config, migrated to `settings_overrides.hooks.SessionStart__extend` ->
  both hook sets present in the managed file.
- Migration: an existing config-dir-targeted `settings_overrides` config -> documented
  behavior change (and narrowing guard fires where it would drop base entries).
