Reworked the desktop app's titlebar and navigation to match the minds-options mockup (https://imbue-ai.github.io/mind-sketches/prototypes/minds-options/):

- The titlebar's left side is now a single "(home icon) Minds" button that grows into a breadcrumb ("Minds / workspace-name") on workspace-scoped screens, with Workspace and Workspace Settings icon-tabs and the workspace switcher menu anchored to the workspace name (the old hamburger menu, trimmed to just the "New workspace" entry). The global back/forward arrows and the centered page title are gone; a contextual back arrow appears only on pages that opt in (the create form, the sharing editor, browser-mode fallbacks). The top-right holds the requests inbox button (with a pending-request count badge) and the report-a-bug button.

- The home screen gained bottom-left launchers ("Minds Settings" and the signed-in account email, or "Log in") that open centered modals on the shared overlay surface in Electron; the full-page /settings and /accounts routes remain as browser-mode fallbacks.

- The Minds Settings surface shows the same app-level settings as the full /settings page -- Connectors, Local files, Workspaces (cross-workspace delegation), Error reporting, and Master password -- as a left-nav + panel layout inside a widened centered modal. The home-screen launcher opens it as a centered overlay with no "back to workspaces" link (dismissed via the X or a backdrop click); the full-page /settings route remains as the browser-mode fallback. The modal is a fixed height (85% of the window) so switching sections never resizes it.

- The centered Minds Settings and Manage Accounts modals render on the shared OverlaySurface wrapper (like the other overlay pages), so they drop the reserved classic-scrollbar gutter and their dim backdrops paint all the way to the window edge.

- Pending permission requests are shown in the inbox side popup: an inbox button in the titlebar's top-right (with a pending-request count badge) opens a master/detail overlay that lists every pending request with its full Approve/Deny form. A new pending request auto-opens the inbox (gated by the auto-open setting); notification, workspace-relay, and deep-link opens land pre-selected on the target request. The per-workspace Connections view, and its connectors / shared-files / workspace-delegation list, were dropped.

- The sign-in modal honors ?return_to= so sign-ins launched from the home screen or the Manage Accounts modal land back where they started; the create-flow default is unchanged.

- The macOS traffic lights now stay visible (as the inactive grey) when a minds window is not the focused window, instead of vanishing -- Electron hides them on blur when a custom traffic-light position is set, so visibility is now re-asserted on focus and blur.

- The workspace switcher menu opens shifted left so each row's workspace-name text lines up directly under the breadcrumb's workspace-name text.

- The workspace switcher rows no longer show the "open in new window" arrow; each local row keeps just its settings gear. The right-click context-menu "Open in new window" entry is unchanged.

- On launch the app no longer tries to restore a workspace window whose workspace no longer exists (which showed the "unresponsive" recovery page); when nothing is known to exist yet, workspace windows are dropped and the app lands on the home screen. Non-workspace screens (home, settings) still restore as before.

- The onboarding flow is now a committed choice: while the user is signed out with no workspaces the home route returns to the welcome splash (Sign Up / Log In / Continue without an account) instead of the create form. Only after signing in or explicitly choosing "Continue without an account" (routed through /welcome/skip) does home lead to the workspace list. The choice is per-run, matching the cold-start routing that lands a functionally-empty app on the splash.

- The welcome splash's Sign Up / Log In now open the centered sign-in modal on the shared overlay surface (Sign Up leads with the sign-up tab, Log In with the sign-in tab via a new mode parameter) instead of navigating to the full-page /auth routes, and the titlebar home button is hidden on the splash -- the user must pick one of the three options to move on. The /auth pages remain as browser-mode fallbacks (without a titlebar back arrow), and the home screen's signed-out "Log in" launcher now also leads with the sign-in tab.

- On screens with no workspace accent (home, settings, sign-in) the titlebar now uses a subtle neutral grey instead of pure white/black, so the inactive (unfocused) macOS traffic lights stay visible instead of washing out against a same-colored strip. Accent-tinted workspace titlebars are unchanged.

- The home screen's "Minds Settings" and account launchers now open their modals. They post through the content relay, but the main process didn't recognize the shell view (which renders the home screen over a parked workspace) as an event source, so the IPC was silently dropped; the shell view is now included when resolving a sender's window.

- The workspace switcher now highlights the workspace you're currently in even on that workspace's own settings or sharing screens (it keys the current-row marker off the active workspace scope, not just the workspace whose content is displayed).

- The titlebar breadcrumb no longer flashes the raw agent id in place of the workspace name: accent updates from the workspace-settings color picker preserved only the accent (dropping the cached name until the next discovery tick), and a cache miss fell back to the agent id. The breadcrumb now keeps the displayed name across accent updates and shows a brief ellipsis placeholder (never the id) for a workspace whose name hasn't arrived yet.
