Fixed: a stopped or unresponsive workspace could get stranded on the "Loading workspace" loader and never advance to the recovery page. The desktop shell decides to show the recovery page from a one-shot "system interface status" event; if the chrome window reloaded after a workspace went stuck, that status was lost and never replayed, so the auto-redirect never fired even though the backend had correctly detected the stuck workspace.

Two changes close the gap:

- The Electron shell now replays the latest non-healthy workspace status when a chrome/sidebar view (re)loads, so a reloaded window re-learns which workspaces are stuck and redirects to the recovery page.

- The backend's chrome event stream now periodically re-asserts non-healthy workspace statuses (in addition to the existing connect-time snapshot and per-transition pushes), so a desynced window self-heals within a few seconds even if it missed the original event.
