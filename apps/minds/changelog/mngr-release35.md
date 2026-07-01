Release minds v0.3.5: bump `apps/minds/package.json` to `0.3.5` and point the shipped binary's `FALLBACK_BRANCH` at the `minds-v0.3.5` forever-claude-template tag. This rolls up all mngr/minds changes that landed on `main` since `minds-v0.3.4`, notably:

- The "get help" flow now spawns an `/assist` chat in the loaded workspace and frames the help modal as an agent submission for agent-escalated reports, with a full loading state that auto-closes on success.

- UI copy renamed from "project" to "workspace" throughout (Login window title, page copy, and the "Back to workspaces" back-link).

- Release tests split by tag: the minds release suite (`minds_deployment` / `minds_services` plus the plain minds `@release` tests) now runs from the minds release job rather than the mngr release workflow.
