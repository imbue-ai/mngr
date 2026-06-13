# Claude settings: hooks + overrides via the per-agent config-dir settings.json

Status: **spec only -- not yet implemented.** Supersedes the earlier `--settings`-merge
design in this file's history. Builds on the `mngr/claude-hook-leak` fix but **reverts most
of its `--settings` machinery** in favor of letting Claude do the layering natively.

Audience: developers in `libs/mngr_claude` (+ `libs/mngr_claude_subagent_proxy`).

> **KNOWN LIMITATION (must be documented in user-facing docs + the field help).**
> This design fully supports `settings_overrides` and a user `--settings` **only in normal
> mode** (mngr provisions a per-agent config dir). In **`use_env_config_dir` mode** there is
> no per-agent config dir to write `settings.json` into, so mngr injects only its own hooks
> via `--settings`, and:
> - a user-supplied `--settings` (in `cli_args`/`agent_args`) will **collide** with mngr's
>   (Claude is last-wins across `--settings` flags) -- one silently clobbers the other;
> - `settings_overrides` is **not applied** in this mode.
>
> This is an accepted, scoped limitation (the mode is not yet used in production -- only
> planned for forever-claude-template's primary-agent swap). It must be called out clearly
> in the `use_env_config_dir` field description and the settings docs so users in that mode
> aren't silently surprised. Lifting it later means adding the `--settings`-merge path back
> for that mode only. See "use_env_config_dir mode" below.

## The core decision

mngr injects everything it owns into the **per-agent config-dir `settings.json`** (the
"user" layer Claude reads from `$CLAUDE_CONFIG_DIR`), and passes **no `--settings`** of its
own. The user's raw `--settings` flag passes through untouched; Claude natively layers it on
top. This works because Claude's native settings layering (verified, v2.1.173):

- **deep-merges nested dicts** across layers (a project-file `env` sibling survives a
  different `env` sibling set via `--settings`), and
- **concatenates same-event hooks** across layers (a project-file `SessionStart` hook and a
  `--settings` `SessionStart` hook both fire).

So mngr does **not** reimplement Claude's merge. It builds one settings file; Claude merges
the user's own layers (`--settings`, work-dir `.claude/settings.local.json`, etc.) over it.

This is leak-safe: the per-agent config dir is `$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/`,
which a plain `claude` run in the work dir never reads (it reads `~/.claude`). The original
bug was hooks in the **work-dir** `.claude/settings.local.json`, which plain `claude` *does*
read.

## Why not the managed `--settings` file (the interim PR approach)

The interim fix put mngr's hooks in a managed file passed via `claude --settings <file>`.
Problem: Claude honors only the **last** `--settings` on the command line (last-wins, no
merge -- verified), so a user's own `--settings` (the reviewer's `cli_args = "--settings
'{...}'"`) collides with mngr's and one silently clobbers the other. The interim fix then
had to extract, unquote, strip, and merge the user's `--settings` into mngr's file -- a lot
of machinery to reimplement what Claude's native layering already does for free when mngr
uses the config-dir `settings.json` instead. **This spec removes that machinery.**

## Expected behavior (normal mode)

### Building the config-dir settings.json

At provision, `settings.json` is built by deep-merging, lowest to highest:

1. **Base:** the synced `~/.claude/settings.json` when `sync_home_settings`, else generated
   defaults.
2. **Unattended flags** (`compute_settings_json_flags`).
3. **mngr's hooks** (always-on readiness; optional credential-sync on macOS; optional
   permission-auto-allow) -- concatenated into the hook event lists.
4. **`settings_overrides`** -- deep-merged so nested siblings survive (the #1647 fix:
   `permissions.allow` from `settings_overrides` does not wipe `permissions.defaultMode`
   from the home base).

The merge is a Claude-aware recursive merge (`deep_merge_settings`, generalized from the
#1647 fix): dict values recurse; **hook-event lists concatenate** (so mngr's and the user's
hooks coexist); other leaves take the higher layer's value. See Open Question 1 on
non-hook leaf lists.

### The user's raw --settings passes through

- mngr no longer injects `--settings`. A user `--settings` in `cli_args` / `agent_args` is
  left in the launch command verbatim (no extraction, unquoting, or stripping).
- Claude layers it (command-line layer, highest) over the config-dir `settings.json`:
  nested dicts deep-merge, same-event hooks concat. So the reviewer's `--settings` adds its
  `SessionStart` hook *and* mngr's readiness hooks still fire -- natively, no mngr code.

### settings_overrides across config scopes (user < project < local)

- Condensed at config-load by the **normal mngr config machinery**: bare key = assign (a
  drop of a non-empty base aggregate still hard-errors via the narrowing guard); `__extend`
  = merge. `__extend` stays **one-level** (a nested `permissions__extend` preserves a
  sibling `defaultMode`); see [config-deep-merge-dict-fields](./config-deep-merge-dict-fields.md)
  -- which concludes no change to `__extend` is needed.
- The condensed `settings_overrides` is then deep-merged into `settings.json` at provision
  (step 4 above). Note this provision-time merge onto the home base is **deep by default**
  (so #1647 works without the user needing `__extend` against home); the bare-vs-`__extend`
  narrowing semantics apply to the cross-*config-scope* merge, not to the
  settings_overrides-onto-home merge. See Open Question 2.

### Schema-free

`settings_overrides` stays `dict[str, Any]`; pydantic never validates Claude's keys. mngr
forwards them verbatim. Claude itself may reject an unknown key; mngr neither catches nor
suppresses that.

### use_env_config_dir mode (reduced support, future work)

In this mode there is no per-agent config dir (Claude reads the user's shared
`$CLAUDE_CONFIG_DIR`), so mngr cannot write a config-dir `settings.json`. mngr still injects
its hooks via `--settings` (the only channel), and the user's shared `~/.claude/settings.json`
is read natively by Claude. Full `settings_overrides` / raw-`--settings`-collision support in
this mode is **out of scope** here (it's the only case that needs the `--settings` merge
machinery, and the mode is not yet used in production -- only planned for
forever-claude-template's primary-agent swap). Revisit if/when that ships.

## Worked examples

Reviewer's config -- works with **no mngr merge code**:
```toml
[agent_types.coder]
cli_args = """--settings '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "..."}]}]}}'"""
```
-> passes through; Claude concats the user's `SessionStart` group with mngr's readiness
group; both fire.

#1647 -- add a permission without wiping a sibling:
```toml
[agent_types.coder.settings_overrides.permissions]
allow = ["Bash(npm *)"]
```
-> deep-merged into `settings.json`; the home base's `permissions.defaultMode` survives.

## Changes

`libs/mngr_claude/imbue/mngr_claude/plugin.py`:

- **Revert the `--settings` machinery** added by the interim fix: remove
  `MANAGED_SETTINGS_LAUNCH_ARG` from the launch command, the `partition_settings_args` /
  `_unquote_cli_settings_value` / `_resolve_user_settings` / `_collect_user_settings_values`
  helpers and their use in `assemble_command` / `_configure_agent_hooks`. The user's
  `cli_args` / `agent_args` flow to `claude` unmodified.
- Move mngr's hooks into `_build_settings_json`: build the hook config and deep-merge it
  (plus `settings_overrides`) into the config-dir `settings.json`. `_configure_agent_hooks`
  (the managed-file writer) is removed or folded in.
- Keep `settings_overrides` deep-merged (not shallow `dict.update`) -- the #1647 fix.

`libs/mngr_claude/imbue/mngr_claude/claude_config.py`:

- Keep `merge_hooks_config` / a recursive `deep_merge_settings` for building `settings.json`.
- Remove `partition_settings_args` / the `--settings`-flag helpers (no longer needed).

`libs/mngr_claude_subagent_proxy`:

- The proxy's hooks that currently target the managed `--settings` file move to the same
  config-dir `settings.json` channel. (Its Stop-hook *guard* on the work-dir
  `settings.local.json` is a separate mechanism and stays; see its gitignore check.)

`use_env_config_dir`: retains a minimal `--settings`-for-hooks path (or is explicitly noted
as reduced-support), per the scope decision above.

Docs / changelog: update mngr_claude settings docs; changelog notes the leak fix now lands
hooks in the per-agent config-dir `settings.json`, `settings_overrides` deep-merges
(#1647), and a user `--settings` passes through and is layered natively by Claude.

## Open questions

1. **Non-hook leaf lists in the build merge.** When deep-merging `settings_overrides` onto
   the home base, a leaf list like `permissions.allow` set in both -- concat or replace? The
   original #1647 `deep_merge_settings` concatenated (skipping dups). Hook-event lists must
   concat. Confirm concat is right for all lists, or distinguish.
2. **settings_overrides-onto-home: deep-by-default vs `__extend`.** This spec deep-merges at
   provision so #1647 works without `__extend`. Earlier discussion leaned toward "same as
   the rest of our settings" (bare = replace + narrow), which would require deferring
   `__extend` resolution to provision against the home base (the `create_templates` pattern)
   -- the complexity this design avoids. Confirm deep-by-default onto home is acceptable
   (the narrowing guard still applies across config scopes).
3. **PR scoping.** This reworks green code on the current branch. Land as a rework of this
   PR, or land the interim `--settings` leak fix and do this as a follow-up? (See the
   accompanying decision.)
