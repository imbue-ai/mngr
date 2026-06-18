Merge intent for an agent type's `settings_overrides` is now declared with a Claude-compatible `__mngr_merge` map instead of the `__extend` / `__assign` key suffixes.

Because `settings_overrides` is folded into a file the external AI CLI also reads (Claude Code's / antigravity's `settings.json`), the suffixes -- which that CLI does not understand and would surface as junk literal keys -- are no longer allowed there. Instead, declare the operator in a single top-level `__mngr_merge` map keyed by dotted path, which the external CLI silently ignores:

```toml
[agent_types.claude.settings_overrides.permissions]
allow = ["Bash(npm *)"]
[agent_types.claude.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"   # merge onto the base; "assign" replaces without the narrowing guard
```

A bare key still assigns with the narrowing guard; the narrowing error now prints the exact `__mngr_merge` patch to add. Raw `__extend` / `__assign` suffix keys under `settings_overrides` are a hard error pointing to `__mngr_merge`. mngr's own (non-`settings_overrides`) config is unchanged and still uses the suffixes.
