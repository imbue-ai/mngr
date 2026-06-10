Workspace color picker -- ongoing work; this entry will grow as more phases land.

- Plan document at `blueprint/workspace-color-picker/plan-workspace-color-picker.md` settling on per-workspace color stored as an mngr `color=<hex>` label on the primary `claude` agent.

- Internal infrastructure for the picker now in place: `WORKSPACE_PALETTE` (12 entries: the 11 named Figma colors at node 356:4113 + `#ffffff`), a WCAG relative-luminance contrast picker (`pick_workspace_foreground`), and a lenient hex normalizer (`normalize_workspace_color`) shipping in a new `workspace_color.py` module. JS mirror in `static/workspace_accent.js`; a drift-guard test enforces lockstep.

- The SSE workspaces payload now carries a stored `color` label per workspace and a server-computed `accent_fg` RGB triple for the contrasting titlebar foreground. Workspaces without a `color` label still fall back to the legacy SHA-derived OKLCH accent and `"0 0 0"` foreground, so existing workspaces look unchanged until the picker UI and the migration backfill land.

- No user-visible behavior shift yet; the picker UI in onboarding and settings is added in follow-up commits on the same branch.
