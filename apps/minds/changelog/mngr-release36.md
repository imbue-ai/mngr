Release minds v0.3.6: bump `apps/minds/package.json` to `0.3.6` and point the shipped binary's `FALLBACK_BRANCH` at the `minds-v0.3.6` forever-claude-template tag. This rolls up all mngr/minds changes that landed on `main` since `minds-v0.3.5`, notably:

- Rebranded desktop-app assets: a new profile-head icon on a maroon/cream palette, an Apple squircle mask, a 1024x1024 PNG so the largest macOS icon slots are no longer upscaled, and a matching startup/quitting/error splash screen.

- Workspaces can be renamed. A new `POST /api/v1/workspaces/<agent_id>/rename` endpoint updates the normalized host name and the human-readable display name together, so the two never drift.

- Modal is selectable as a compute provider in the create form ("Modal (1-day ephemeral)"), authenticating from the local machine with its own token.

- Titlebar and landing-page action buttons show custom styled tooltips on hover and keyboard focus, replacing the unreliable OS `title=` tooltips, and the desktop client's overlay layer is unified onto a single always-warm surface.

- The permission-request dialog shows a spinner and disables both buttons while an approval is processed in the background, so a browser sign-in can't be double-submitted.

- Discovery consumption moved to mngr's per-provider model: one slow or erroring provider no longer disrupts the others, and freshness is tracked per provider.

- Secrets are redacted from persisted logs -- the latchkey gateway password, the permissions-override JWT, `modal secret create` values, and the `mngr forward` preauth cookie no longer reach the JSONL log, `ProcessError` messages, or `minds.log`.

- Fixed the workspace-recovery flow stranding users on "Workspace unresponsive" after the computer wakes from sleep, "Destroying..." spinning forever for cloud workspaces, "Restart workspace" failing on providers that cannot stop a host in place (Modal), the "Report a bug" button doing nothing on the full-app error screen, the bug-report text field losing focus once per second over a loading workspace, and a slow memory leak in the discovery consumer.

- Bumped bundled Latchkey to 2.20.0. Removed the legacy OVH-VPS pool-host path from the minds env tooling (`minds pool create` is slice-only; the direct OVH provider is unaffected).
