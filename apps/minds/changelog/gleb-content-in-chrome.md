Trusted local pages in the desktop client now render the app titlebar directly, moving toward a content surface that hosts agent content only.

- Extracted the trusted app shell (the fixed titlebar, the browser-mode floating sidebar, and the accent / `chrome.js` wiring) into a reusable `ChromeShell.jinja` layout component. `Chrome.jinja` (the agent content surface) is now a thin wrapper around it.

- Every trusted local page (Landing, Create, Settings, Accounts, Consent, Welcome, Creating, Destroying, workspace settings, sharing, and the auth-flow pages) now renders that shared titlebar itself, sitting below it in a neutral, full-bleed surface (no accent-tinted content card). `chrome.js` gained a "local page" mode: on a page that is its own main frame (no content iframe), the titlebar's Home / Back / Forward / sidebar navigate the whole page rather than driving a child iframe or the content view.

- The Landing page's "open in new window" and "stop workspace" actions, the Create page's sign-in prompt, and the workspace-settings color picker now call the desktop shell bridge directly (they are trusted local pages on the chrome surface), instead of routing through the caged content-view relay.

- In the desktop app, the two surfaces are now split by content type. The chrome view renders the titlebar and navigates among the trusted local pages directly; the content view hosts workspace (agent) content only, is shown while you are on a workspace, and is hidden and unloaded when you leave it (so no workspace keeps running behind a local page). Navigating Home / Create / Settings shows the local page with no workspace card; opening a workspace shows it tinted with the workspace accent. Back/forward act on whichever surface is showing.

- Backing out of a quit (choosing "Shut down all", then "Cancel quit" when a mind can't be stopped) now returns each window to the exact page it was on -- including trusted local pages -- instead of leaving a local-page window on a blank agent wrapper.

- Locked down the content-view relay now that the trusted local pages no longer use it: the allowlist a workspace (agent) page can post through it is down to just the two affordances foreign content legitimately needs -- opening a pending permission request and opening the report-a-bug modal. The sign-in-modal, stop-mind, open-in-new-window, and titlebar-accent-preview messages are gone from it (they are shell-bridge calls from trusted pages now, unreachable from agent content).

- Added a defense-in-depth guard so the content view can never navigate to a trusted minds page: an in-page attempt to load a bare backend-origin URL there is blocked (trusted pages only ever render on the chrome surface).

- Opening a workspace from a trusted local page now always routes through the desktop shell so the workspace lands on the (caged) content surface: clicking a workspace row on the Landing page and the redirect when a new workspace finishes creating hand the `/goto/<agent>/` URL to the shell's navigate-content bridge (which also focuses an existing window already on that workspace) instead of navigating the page's own frame. Previously, because these pages now render on the chrome surface, that frame navigation loaded the workspace's (untrusted) agent content into the trusted chrome view. Added the symmetric guard so the chrome view can never navigate to agent content, mirroring the content-view guard above.

- Simplified the desktop auth-cookie handling: sign-in now happens entirely on the trusted default session (login page + sign-in modal on the chrome/modal surfaces), so the old content-partition-to-default `minds_session` watcher was removed; the authenticated cookie is still pushed to the content partition so `/goto` forwarding stays authenticated.
