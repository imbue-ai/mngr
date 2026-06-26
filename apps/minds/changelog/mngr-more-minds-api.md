Continues the minds workspace-API work and merges the latest `main` into the branch.

- Adds a versioned `/api/v1/workspaces` cross-workspace management API that lets an agent in one workspace act on others: list, get detail, version, list/export backups, create, destroy, start/stop (with operation-status polling and SSE operation logs), and establish SSH access to remote targets. All routes are dual-authenticated (central `MINDS_API_KEY` bearer or session cookie).

- Reconciles with `main`: keeps `main`'s new per-agent bug-report route (`POST /api/v1/agents/<id>/report`) and adopts `main`'s removal of the create-flow onboarding questions (the onboarding module, endpoint, and the related `UserDataPreference` enum are gone; the create form now goes straight to the setting-up screen). The legacy `/api/create-agent` JSON route is removed in favor of the v1 surface.
