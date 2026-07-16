Reworked the desktop app's titlebar and navigation to match the minds-options mockup (https://imbue-ai.github.io/mind-sketches/prototypes/minds-options/):

- The titlebar's left side is now a single "(home icon) Minds" button that grows into a breadcrumb ("Minds / workspace-name") on workspace-scoped screens, with Workspace and Workspace Settings icon-tabs and the workspace switcher menu anchored to the workspace name (the old hamburger menu, trimmed to just the "New workspace" entry). The global back/forward arrows and the centered page title are gone; a contextual back arrow appears only on pages that opt in (the sharing editor, browser-mode fallbacks). The top-right holds the requests inbox button (with a pending-request count badge) and the report-a-bug button.

- The home screen gained bottom-left launchers ("Minds Settings" and the signed-in account email, or "Log in") that open centered modals on the shared overlay surface in Electron; the full-page /settings and /accounts routes remain as browser-mode fallbacks.

- The Minds Settings surface shows the same app-level settings as the full /settings page -- Connectors, Local files, Workspaces (cross-workspace delegation), Error reporting, and Master password -- as a left-nav + panel layout inside a widened centered modal. The home-screen launcher opens it as a centered overlay with no "back to workspaces" link (dismissed via the X or a backdrop click); the full-page /settings route remains as the browser-mode fallback. The modal is a fixed height (85% of the window) so switching sections never resizes it.

- The centered Minds Settings and Manage Accounts modals render on the shared OverlaySurface wrapper (like the other overlay pages), so they drop the reserved classic-scrollbar gutter and their dim backdrops paint all the way to the window edge.

- Pending permission requests are shown in the inbox side popup: an inbox button in the titlebar's top-right (with a pending-request count badge) opens a master/detail overlay that lists every pending request with its full Approve/Deny form. A new pending request auto-opens the inbox (gated by the auto-open setting); notification, workspace-relay, and deep-link opens land pre-selected on the target request. The per-workspace Connections view, and its connectors / shared-files / workspace-delegation list, were dropped.

- The sign-in modal honors ?return_to= so sign-ins launched from the home screen or the Manage Accounts modal land back where they started; the create-flow default is unchanged.

- The macOS traffic lights now stay visible (as the inactive grey) when a minds window is not the focused window, instead of vanishing -- Electron hides them on blur when a custom traffic-light position is set, so visibility is now re-asserted on focus and blur.

- The workspace switcher menu opens shifted left so each row's workspace-name text lines up directly under the breadcrumb's workspace-name text.

- The workspace switcher rows for OTHER workspaces carry an "open in new window" arrow (desktop app only); the current workspace's row and remote rows carry no action buttons. The per-row settings gear is gone -- workspace settings lives in the titlebar's settings tab. The right-click context-menu "Open in new window" entry is unchanged.

- The workspace switcher rows now show the mind's status with the mockup's icons: a closed-eye icon on stopped minds and an alert triangle on minds whose status is unknown (each with a tooltip); running minds show nothing. Uses the liveness already carried by the workspace list for shutdown-capable local workspaces.

- The rotating tips on the workspace-creation loading screen swap every 8 seconds instead of every 3, so each tip can be read comfortably before the next appears.

- On launch the app no longer tries to restore a workspace window whose workspace no longer exists (which showed the "unresponsive" recovery page); when nothing is known to exist yet, workspace windows are dropped and the app lands on the home screen. Non-workspace screens (home, settings) still restore as before.

- The onboarding flow is now a committed choice: while the user is signed out with no workspaces the home route returns to the welcome splash (Sign Up / Log In / Continue without an account) instead of the create form. Only after signing in or explicitly choosing "Continue without an account" (routed through /welcome/skip) does home lead to the workspace list. The choice is per-run, matching the cold-start routing that lands a functionally-empty app on the splash.

- The welcome splash's Sign Up / Log In now open the centered sign-in modal on the shared overlay surface (Sign Up leads with the sign-up tab, Log In with the sign-in tab via a new mode parameter) instead of navigating to the full-page /auth routes, and the titlebar home button is hidden on the splash -- the user must pick one of the three options to move on. The /auth pages remain as browser-mode fallbacks (without a titlebar back arrow), and the home screen's signed-out "Log in" launcher now also leads with the sign-in tab.

- On screens with no workspace accent (home, settings, sign-in) the titlebar now uses a subtle neutral grey instead of pure white/black, so the inactive (unfocused) macOS traffic lights stay visible instead of washing out against a same-colored strip. Accent-tinted workspace titlebars are unchanged.

- The home screen's "Minds Settings" and account launchers now open their modals. They post through the content relay, but the main process didn't recognize the shell view (which renders the home screen over a parked workspace) as an event source, so the IPC was silently dropped; the shell view is now included when resolving a sender's window.

- The workspace switcher now highlights the workspace you're currently in even on that workspace's own settings or sharing screens (it keys the current-row marker off the active workspace scope, not just the workspace whose content is displayed).

- The sharing editor now opens as a centered popup on the shared overlay surface when launched from Workspace Settings' "Manage sharing" buttons in the desktop app (dismissed via Cancel, the X, or a backdrop click; the popup's workspace/account names are plain text so nothing can navigate the overlay). The full /sharing page remains as the browser-mode fallback.

- The titlebar breadcrumb no longer flashes the raw agent id in place of the workspace name: accent updates from the workspace-settings color picker preserved only the accent (dropping the cached name until the next discovery tick), and a cache miss fell back to the agent id. The breadcrumb now keeps the displayed name across accent updates and shows a brief ellipsis placeholder (never the id) for a workspace whose name hasn't arrived yet.

- The sharing editor behaves exactly as the full page always has (a "Loading..." line until the status fetch resolves, then the editor with the access list), with one difference: Update/Disable no longer reload the page -- the editor grays out while the save runs, then refreshes in place from the existing sharing-status endpoint. This removes the popup's blank-and-repaint after every save (a reload would blank the overlay iframe) and applies to the full-page browser fallback too.

- The sharing popup's card now has a stable height (70% of the window, like the settings modal's fixed-height treatment but proportionate to the smaller editor) with the heading pinned and the editor body scrolling inside, so state changes never resize the card.
