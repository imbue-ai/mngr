`settings_overrides` now folds onto the base with the same principled merge as mngr_claude: a bare key assigns with a narrowing guard (errors if it would silently drop a non-empty list/dict/set from the base), and a top-level `__mngr_merge` map declares per-key `extend` (merge onto the base) or `assign` (replace without the guard).

```toml
[agent_types.my_antigravity.settings_overrides.permissions]
allow = ["command(git)"]
[agent_types.my_antigravity.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"
```

`__mngr_merge` is ignored by vanilla antigravity, so the generated `settings.json` stays clean. Raw `__extend` / `__assign` suffix keys are rejected in `settings_overrides`, and a `__mngr_merge` key in the synced home settings base is stripped. On a narrowing, the error prints the exact `__mngr_merge` patch to add (the full nested patch: `extend` for a dict that would drop a sibling key, `assign` for a replaced list/value). Previously `settings_overrides` replaced top-level keys wholesale with no narrowing guard.
