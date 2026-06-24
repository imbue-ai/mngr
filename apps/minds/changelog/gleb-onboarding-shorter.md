Show the welcome / sign-in screen whenever the desktop app is functionally empty -- signed out of every account and with no workspaces -- so signed-out users are nudged to sign in again before using the app.

Previously a leftover window from a prior session (e.g. a plain home/`/` window) counted as "restorable" at startup and silently reopened, landing the user on the create page even with no accounts and no workspaces. The cold-start landing decision now treats "no accounts AND no workspaces" as empty and routes to `/welcome` regardless of any stale window-state. A signed-out user who still has workspaces is unaffected (they land on home / their restored windows, not a welcome wall).

The startup landing precedence (welcome > create > restore) was extracted into a pure `electron/startup-routing.js` helper and is covered by `node:test` unit tests (`pnpm --dir apps/minds test:unit`).

"Continue without an account" on the welcome screen now goes straight to the create page instead of first opening a confirmation dialog explaining what an account unlocks.
