# Consolidated Claude settings via mngr's assign/extend/narrowing semantics

Status: **spec only -- not yet implemented.** Extends the `mngr/claude-hook-leak` PR
(which fixed the `--settings` collision; see "Background"). Collapses mngr's two Claude
settings artifacts (config-dir `settings.json` + managed `--settings` file) into the single
managed `--settings` file, and routes user overrides through mngr's config-merge model.
Builds directly on [env-settings-overrides](./env-settings-overrides/concise.md), reusing
its `resolve_extends`, narrowing guard, and `parse_scalar_value`.

Audience: developers implementing the change in `libs/mngr_claude` (and a small reuse
of `libs/mngr` config helpers).

## Overview

- **Consolidate onto a single mngr-owned settings file.** Stop writing the per-agent
  config-dir `settings.json` (`_build_settings_json`). Everything mngr injects --
  generated defaults / the synced `~/.claude/settings.json`, unattended flags,
  `settings_overrides`, the user's raw `--settings`, and mngr's runtime hooks -- is
  built into the one managed `--settings` file at provision. This unifies the normal and
  `use_env_config_dir` modes (today only the latter relies on `--settings`) and removes
  the "two settings artifacts that each carry part of the picture" split.
- Route the user-controllable parts (`settings_overrides`, raw `--settings`) through
  mngr's **existing** config-merge model -- assign-by-default, opt-in `__extend`, and the
  narrowing guard -- instead of the special-purpose `deep_merge_settings` (hook
  concatenation) added by the collision fix.
- Stay **schema-free**: `ClaudeAgentConfig.settings_overrides` remains `dict[str, Any]`, so
  pydantic never enumerates or validates Claude's `settings.json` keys. The config-merge
  machinery (`resolve_extends`, narrowing) operates *structurally* over whatever nesting the
  TOML tables / JSON produce -- it descends into nested tables and resolves inner `__extend`
  just as it does for the existing open `commands.<cmd>.defaults: dict[str, Any]` field. A
  Claude key mngr doesn't know about is simply forwarded.
- Let the user **control merge recursion depth via TOML nesting**: a nested TOML table
  recurses (sibling-preserving); a JSON-blob string leaf is parsed and treated as a
  **collection** value subject to the narrowing guard -- not an opaque scalar.
- Treat the raw `--settings` flag (in `cli_args` / `agent_args`) as a single one-level
  override under the same rules. The flag still must be **stripped and folded in**: two
  `--settings` flags collide (last-wins; see "Background"), so the user's value cannot
  simply pass through alongside mngr's.

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
- **The config-dir `settings.json` today** (`_build_settings_json`): mngr writes a
  `settings.json` into the per-agent config dir carrying (a) generated defaults or, when
  `sync_home_settings`, a copy of the user's `~/.claude/settings.json`; (b) unattended
  flags (`compute_settings_json_flags`); (c) `settings_overrides` via shallow `dict.update`.
  This is the channel by which the user's own home settings reach the agent in normal mode.
  (The config *dir* also holds `.claude.json`, `installedPlugins`, marketplace data, and
  plugin-path rewrites -- those are **not** affected here; only `settings.json` is removed.)
- **The collision fix (this PR)**: mngr's hooks moved out of the project's
  `settings.local.json` into a private per-agent managed `--settings` file
  (`$MNGR_AGENT_STATE_DIR/plugin/claude/mngr_managed_settings.json`). A user `--settings`
  from `cli_args`/`agent_args` is currently deep-merged (hook-concat) into that file via
  `deep_merge_settings` and stripped from Claude's argv.

### Empirical findings (native Claude, v2.1.173)

Verified with isolated `CLAUDE_CONFIG_DIR` runs (hooks/settings resolve at session start,
before auth, so a "Not logged in" exit still exercises resolution):

- **Two `--settings` flags collide -- last-wins, full replacement, no merge.** This is why
  a user `--settings` must be stripped and folded into mngr's single managed file rather
  than passed as a second flag.
- **`--settings` is an additive layer over the file hierarchy.** A `SessionStart` hook in a
  project `.claude/settings.json` and a *different* `SessionStart` hook in `--settings`
  **both fire** (`managed > --settings > local > project > user`). So Claude still natively
  layers project/local/enterprise settings on top of mngr's `--settings` file.
- **`--settings` faithfully carries non-hook keys.** An `env` block passed via `--settings`
  took effect (a hook saw the injected variable) exactly as it would from `settings.json`.
  This is what licenses moving the config-dir `settings.json`'s contents into the managed
  `--settings` file. (Caveat: `env` + `hooks` were tested as representatives of the
  settings-layer mechanism; no exhaustive per-key audit -- low residual risk that an
  obscure key is on-disk-only.)

## Expected behavior

### The managed file's base (built at provision)

The single managed `--settings` file is assembled at provision from these layers, lowest
to highest, by plain `deep_assign` (later layers win per leaf; the user-controllable layers
additionally go through `__extend`/narrowing -- see below):

1. **Generated defaults**, or the synced `~/.claude/settings.json` when `sync_home_settings`
   (today's `_build_settings_json` base).
2. **Unattended flags** (`compute_settings_json_flags`).
3. **mngr's runtime hooks** (always-on readiness, plus optional credential-sync and
   permission-auto-allow). These are the entries the narrowing guard most wants to protect.

This composite is the **base** that `settings_overrides` and the raw `--settings` flag
then merge onto.

### The override merge model (uniform with mngr config)

- **Base** = the composite above (home settings + flags + mngr hooks).
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

> **REVISED (pending architecture lock).** Two corrections from review, to be folded in once
> the deferral approach is settled:
> 1. **No `deep_assign`.** Reuse the existing machinery: `resolve_extends(provision_base,
>    settings_overrides)` resolves `__extend` against the base (the depth lives inside
>    `_apply_extend`), then a trivial **top-level** `{**provision_base, **resolved}` overlays
>    the override's keys. The overlay is shallow precisely because `resolve_extends` already
>    did the depth -- same as how `merge_with` overlays at config-load. No new merge function.
> 2. **Deferral only where the base is runtime-built.** Cross-config-scope condensation
>    (user < project < local `settings_overrides`) happens at config-load via normal
>    `resolve_extends` + `merge_with` (a left fold). Deferral to provision is needed only for
>    `__extend` that must resolve against the **provision base** (synced home settings + mngr
>    hooks), which doesn't exist at config-load. Make this a **canonical shared "deferred
>    `__extend` paths"** mechanism (generalize the `create_templates` carveout in
>    `key_resolver.py`, and de-dup `_apply_template_extend` into `_apply_extend`), not a
>    second special case.

mngr's hooks are **built at provision time**, not present as a config layer at config-load.
So `settings_overrides`' `__extend` keys that target mngr's hooks/home base cannot resolve at
config-load -- a `hooks.SessionStart__extend` is meant to extend **mngr's runtime readiness
hooks**, which don't exist yet during config loading.

Therefore:

- Such `settings_overrides` `__extend` keys are **deferred**: `resolve_extends` preserves them
  through config-load (following the `create_templates` precedent), and mngr resolves them
  at **provision time** via `resolve_extends(provision_base, settings_overrides)`, then
  overlays the result top-level onto `provision_base` (see REVISED note above).
- Run the narrowing check (`would_assignment_narrow` / `detect_settings_narrowing`) against
  `provision_base`; on any violation, raise the standard narrowing error unless the escape
  hatch is set.

**Note:** because resolution is deferred to provision, the base for `__extend` and for
narrowing is **mngr's built hooks**, which is precisely what makes "extend mngr's
SessionStart" work and "silently replace mngr's SessionStart" fail loudly.

### One file, both modes

- mngr writes exactly one settings artifact: the managed `--settings` file, assembled in
  `_configure_agent_hooks`. The config-dir `settings.json` is no longer written.
- This works identically in normal and `use_env_config_dir` mode (the managed file lives in
  the agent state dir, always per-agent), removing the mode-specific branch.
- **Precedence change:** the user's home settings + overrides move from the config-dir
  `settings.json` layer to the `--settings` layer of Claude's hierarchy
  (`managed > --settings > local > project > user`). For keys the user *also* sets in a
  project/local `.claude/settings.json`, those file layers now sit **below** `--settings`,
  so mngr's composite wins where they overlap (it previously sat at the config-dir
  `settings.json` level). Document in the changelog; see the `sync_home_settings` resolved
  decision below.

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

### Claude's hook schema (important for the examples)

A hook event is a **list of groups**, each group a dict with an optional `matcher` and a
`hooks` list. mngr emits a singleton group, e.g.:

```json
"SessionStart": [ { "hooks": [ {"type": "command", "command": "<mngr readiness>"} ] } ]
```

For a given event, Claude runs **every** group whose `matcher` matches (no matcher =
always). So a non-singleton `SessionStart` list is just multiple independent hook groups,
all active. This means:

- Adding a user hook = **appending a group to the event list** (a *list* concat), not a dict
  merge. The additive operator is therefore `SessionStart__extend = [ {group} ]`.
- You cannot target mngr's existing group's inner `hooks` list via a dotted path
  (`SessionStart.hooks__extend`) -- `SessionStart` is a **list**, and dotted paths have no
  array index. Append a new group instead; both groups' hooks run.

### Worked examples

Reviewer's case -- add a `SessionStart` hook without dropping mngr's readiness hooks (append
a group via list-concat `__extend`):

```toml
[agent_types.coder.settings_overrides.hooks]
SessionStart__extend = '[{"hooks": [{"type": "command", "command": "..."}]}]'
```
-> appends a second group; mngr's group and the user's group both run.

```toml
[agent_types.coder.settings_overrides]
model = "opus"        # scalar assign, no narrowing
```

Preserve a sibling under a nested dict (the #1647 case) -- `__extend` at the nested level:

```toml
[agent_types.coder.settings_overrides.permissions__extend]
allow = ["Bash(npm *)"]   # mngr/home permissions.defaultMode survives the one-level merge
```

```toml
[agent_types.coder.settings_overrides.hooks]
SessionStart = '[{"hooks": [{"type": "command", "command": "..."}]}]'
```
-> bare assign replaces mngr's `SessionStart` list (drops its group) -> **narrowing error**.
Fix by using `SessionStart__extend`, or set `allow_settings_key_assignment_narrowing`.

Raw flag full-replace -> **narrowing error** (drops mngr's `hooks`):
```
mngr create coder -- --settings '{"hooks": {"SessionStart": [...]}}'
```

### Schema-free guarantee

No code path enumerates Claude's settings keys. `resolve_extends`,
`would_assignment_narrow`/`detect_settings_narrowing`, and `parse_scalar_value` operate
structurally over `Mapping` / `list` / `set`. pydantic does not validate the inner content of
`settings_overrides`: a mistyped Claude key is not validated *by mngr* -- we forward it
verbatim. Claude itself may reject an unknown key; mngr neither catches nor suppresses that.
This is the intended trade-off of schema-free passthrough (mngr does not track Claude's
settings schema).

## Changes

`libs/mngr_claude`:

- `ClaudeAgentConfig.settings_overrides`: update the field description -- assign + `__extend`
  with narrowing; merged into the managed `--settings` file; examples updated.
- `_configure_agent_hooks` becomes the single builder of the managed file. Assemble the
  base by `deep_assign`-ing, in order: generated-defaults-or-synced-home-settings ->
  unattended flags -> mngr's runtime hooks. Then apply `settings_overrides` and the resolved
  raw `--settings` blob(s) via `resolve_extends(base, override)` -> `deep_assign` ->
  `would_assignment_narrow` guard. Replace the `deep_merge_settings` call.
- `_build_settings_json`: **stop writing the config-dir `settings.json`** -- fold its base
  construction (defaults / `sync_home_settings` seeding, `compute_settings_json_flags`) into
  the managed-file builder above and remove the `settings.json` entry from `generated_files`.
  Keep the config dir's other generated files (`.claude.json`, installed-plugins/marketplace
  rewrites) untouched.
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

## Resolved decisions

- **Narrowing escape hatch:** reuse the existing global
  `allow_settings_key_assignment_narrowing`. No per-agent-type flag.
- **File-path `--settings` on remote hosts:** read via the host; missing/unreadable file or
  invalid JSON raises `UserInputError`; the narrowing guard applies to the file's parsed
  content identically to inline JSON.
- **`sync_home_settings`:** the user's home settings are no longer a separate config-dir
  `settings.json` layer -- they are folded into the managed file's base (layer 1), so mngr's
  hooks/flags/overrides `deep_assign` on top of them. The user's *project/local*
  `.claude/settings.json` remain native file layers below `--settings` (Claude merges them;
  see "One file, both modes" for precedence).

## Open questions

1. **Cross-layer `settings_overrides.__extend`.** How do `x__extend` keys from multiple
   config layers (user / project / local / env / `--setting`) combine *before* the
   provision-time resolution against mngr's base? Options: (a) stack them additively across
   layers as raw `__extend` ops, resolve once at provision; (b) resolve cross-layer extends
   at config-load against each other, defer only the final extend against mngr's base.
   Prefer (a) for a single, predictable resolution point. (Low stakes -- only matters when
   the *same* `__extend` key is set in two layers.)
2. **Deferred-resolution mechanism.** Extend `resolve_extends`' create-template carveout
   into a generic "deferred paths" set that includes `settings_overrides` (under any
   `agent_types.<name>` / plugin path), or add a dedicated preserve-path. Prefer a small
   generic mechanism so the two deferred cases share code.
3. **`settings_overrides` vs raw `--settings` ordering.** When both are present, define
   which applies first (and thus what the other narrows against). Proposal: apply
   `settings_overrides` first, then the raw flag, so an explicit per-invocation flag layers
   on top -- both guarded against mngr's base.

## Tests

- `deep_assign` unit: recurse, replace leaves/lists, preserve siblings, no mutation.
- `resolve_extends` applied to Claude-settings dicts: `hooks.SessionStart__extend` concat
  onto a built base; `model` scalar assign; nested-table recursion preserving base events.
- Narrowing: hard error on `hooks`/`hooks.SessionStart` assign that drops mngr entries;
  passes on `__extend` superset and on pure additions; scalar assign exempt; escape hatch
  bypasses.
- Raw `--settings` blob: full-replace -> narrowing error; additive (`__extend` or
  non-overlapping keys) -> ok; file-path value resolved and guarded.
- Base assembly: synced-home-settings (`sync_home_settings`) + unattended flags + mngr
  hooks `deep_assign` into the managed file in order; no config-dir `settings.json` is
  written (assert it's absent from `generated_files`).
- `use_env_config_dir`: the managed file is built identically (and nothing is written to the
  user's real config dir).
- Reviewer's exact config, migrated to `settings_overrides.hooks.SessionStart__extend` ->
  both hook sets present in the managed file; a synced home `env`/`model` also survives.
- Migration: an existing config-dir-targeted `settings_overrides` config -> documented
  behavior change (and narrowing guard fires where it would drop base entries).
