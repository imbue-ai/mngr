Trusted local pages in the desktop client now render the app titlebar directly, moving toward a content surface that hosts agent content only.

- Extracted the trusted app shell (the fixed titlebar, the browser-mode floating sidebar, and the accent / `chrome.js` wiring) into a reusable `ChromeShell.jinja` layout component. `Chrome.jinja` (the agent content surface) is now a thin wrapper around it.

- Every trusted local page (Landing, Create, Settings, Accounts, Consent, Welcome, Creating, Destroying, workspace settings, sharing, and the auth-flow pages) now renders that shared titlebar itself, sitting below it in a neutral, full-bleed surface (no accent-tinted content card). `chrome.js` gained a "local page" mode: on a page that is its own main frame (no content iframe), the titlebar's Home / Back / Forward / sidebar navigate the whole page rather than driving a child iframe or the content view.

- The Landing page's "open in new window" and "stop workspace" actions, and the Create page's sign-in prompt, now call the desktop shell bridge directly when available (falling back to the previous relay path during the transition), instead of always routing through the caged content-view relay.

- In the desktop app, the two surfaces are now split by content type. The chrome view renders the titlebar and navigates among the trusted local pages directly; the content view hosts workspace (agent) content only, is shown while you are on a workspace, and is hidden and unloaded when you leave it (so no workspace keeps running behind a local page). Navigating Home / Create / Settings shows the local page with no workspace card; opening a workspace shows it tinted with the workspace accent. Back/forward act on whichever surface is showing.

- Backing out of a quit (choosing "Shut down all", then "Cancel quit" when a mind can't be stopped) now returns each window to the exact page it was on -- including trusted local pages -- instead of leaving a local-page window on a blank agent wrapper.
