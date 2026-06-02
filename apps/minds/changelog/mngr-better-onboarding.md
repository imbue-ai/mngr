Reworked the workspace creation flow into a guided onboarding experience.

- The Create Workspace form is now name-first: just a workspace name and a Create button up front, with a "Configure..." disclosure for the compute / AI / backup providers and a nested "Show advanced settings" disclosure for the repository, branch, and GH_TOKEN. The account selector moved to a compact menu at the top right.
- After clicking Create, the workspace is created in the background while the user answers three short onboarding questions. If creation finishes before they're done, they go straight into the workspace; otherwise they see a styled loading screen with a progress bar, rotating tips, and a "Show details" toggle over the live creation log.
- The three questions wire up minimal behavior (each is optional):
  - "Is it OK if I get to know you?" runs a small local scan of your machine (your name) and saves it to `~/.minds/user_context/<creation-id>.json` unless you choose full control.
  - "What should we start with?" sends your description to the workspace's chat agent once it comes online.
  - "How do you want to deal with permissions?" is written into the workspace's Claude memory at `runtime/memory/permissions_preferences.md`.
- `POST /api/create-agent` now accepts optional `user_data_preference`, `initial_problem`, and `permissions_preference` fields; omitting them preserves the previous behavior. A new `POST /api/create-agent/{id}/onboarding` endpoint backs the form flow.
