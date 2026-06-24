Show the welcome / sign-in screen whenever the desktop app is functionally empty -- signed out of every account and with no workspaces -- so signed-out users are nudged to sign in again before using the app.

Previously a leftover window from a prior session (e.g. a plain home/`/` window) counted as "restorable" at startup and silently reopened, landing the user on the create page even with no accounts and no workspaces. The cold-start landing decision now treats "no accounts AND no workspaces" as empty and routes to `/welcome` regardless of any stale window-state. A signed-out user who still has workspaces is unaffected (they land on home / their restored windows, not a welcome wall).

The startup landing precedence (welcome > create > restore) was extracted into a pure `electron/startup-routing.js` helper and is covered by `node:test` unit tests (`pnpm --dir apps/minds test:unit`).

"Continue without an account" on the welcome screen now goes straight to the create page instead of first opening a confirmation dialog explaining what an account unlocks.

Reworked the "create a mind" screen into a simpler two-step flow. Instead of a name + color + a stack of provider dropdowns, you now just choose where to run the mind: "Imbue Cloud" (recommended) or "Directly on your computer", as two preset cards. The full provider / repository / branch configuration is still available behind an "Advanced Configuration" link on the same page (with a "Back to simple configuration" link to return); picking a card just fills those advanced fields with that preset's defaults.

The workspace name and color are now chosen automatically -- a generated name (the same coolname style used elsewhere) and the first unused palette color -- so neither is asked for on the create screen.

Choosing the Imbue Cloud (remote) option without a signed-in account now takes you into the sign-in / sign-up flow, with an explainer about what running on Imbue Cloud needs and a one-click link back to the picker; after signing in you land back on the create screen.

The "Imbue Cloud" (remote) card is now selected by default on the create screen for everyone, including users without an account -- previously a user without an account landed with the local "Directly on your computer" card preselected.
