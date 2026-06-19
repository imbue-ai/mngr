Merge intent for an agent type's `settings_overrides` is now declared with a Claude-compatible `__mngr_merge` map instead of the `__extend` / `__assign` key suffixes.

Because `settings_overrides` is folded into a file the external AI CLI also reads (Claude Code's / antigravity's `settings.json`), the suffixes -- which that CLI does not understand and would surface as junk literal keys -- are no longer allowed there. Instead, declare the operator in a single top-level `__mngr_merge` map keyed by dotted path, which the external CLI silently ignores:

```toml
[agent_types.claude.settings_overrides.permissions]
allow = ["Bash(npm *)"]
[agent_types.claude.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"   # merge onto the base; "assign" replaces without the narrowing guard
```

A bare key still assigns with the narrowing guard; the narrowing error now prints the exact `__mngr_merge` patch to add. The suggested patch is the full nested one in a single error: a dict that would drop a sibling key is suggested as `extend` (so the sibling survives) and a replaced list/value as `assign` (so your exact value is kept, not silently broadened). Raw `__extend` / `__assign` suffix keys under `settings_overrides` are a hard error pointing to `__mngr_merge`. mngr's own (non-`settings_overrides`) config is unchanged and still uses the suffixes.

New `mngr config assign <key> <value>` command, mirroring `mngr config extend`: it writes a `key__assign` entry (replace without the narrowing guard), or -- on a `settings_overrides` path -- a `__mngr_merge` `assign` directive. `mngr config set key__assign <value>` routes to it, and `mngr config get` resolves the `__assign` form.

A settings key that contains a literal dot (e.g. an MCP server name like `my.server`) cannot be targeted by a dotted `__mngr_merge` path: such a directive errors as dangling and the auto-remediation skips it rather than mis-advising.

`mngr config set` / `extend` / `assign` now let configuration errors render through the central CLI error handler (a `ConfigParseError` is a `MngrError`) instead of a local catch, so an invalid value prints e.g. `Error: Unknown configuration fields: ['provider']` rather than an `Invalid configuration: ...` line.
