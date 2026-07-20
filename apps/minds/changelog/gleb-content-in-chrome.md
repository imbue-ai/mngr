Trusted local pages in the desktop client now render the app titlebar directly, moving toward a content surface that hosts agent content only.

- Extracted the trusted app shell (the fixed titlebar, the browser-mode floating sidebar, and the accent / `chrome.js` wiring) into a reusable `ChromeShell.jinja` layout component. `Chrome.jinja` (the agent content surface) is now a thin wrapper around it.

- Every trusted local page (Landing, Create, Settings, Accounts, Consent, Welcome, Creating, Destroying, workspace settings, sharing, and the auth-flow pages) now renders that shared titlebar itself, sitting below it in a neutral, full-bleed surface (no accent-tinted content card). `chrome.js` gained a "local page" mode: on a page that is its own main frame (no content iframe), the titlebar's Home / Back / Forward / sidebar navigate the whole page rather than driving a child iframe or the content view.

- The Landing page's "open in new window" and "stop workspace" actions, the Create page's sign-in prompt, and the workspace-settings color picker now call the desktop shell bridge directly (they are trusted local pages on the chrome surface), instead of routing through the caged content-view relay.

- In the desktop app, the two surfaces are now split by content type. The chrome view renders the titlebar and navigates among the trusted local pages directly; the content view hosts workspace (agent) content only, is shown while you are on a workspace, and is hidden and unloaded when you leave it (so no workspace keeps running behind a local page). Navigating Home / Create / Settings shows the local page with no workspace card; opening a workspace shows it tinted with the workspace accent. Back/forward act on whichever surface is showing.

- Backing out of a quit (choosing "Shut down all", then "Cancel quit" when a mind can't be stopped) now returns each window to the exact page it was on -- including trusted local pages -- instead of leaving a local-page window on a blank agent wrapper.

- Locked down the content-view relay now that the trusted local pages no longer use it: the allowlist a workspace (agent) page can post through it is down to just the two affordances foreign content legitimately needs -- opening a pending permission request and opening the report-a-bug modal. The sign-in-modal, stop-mind, open-in-new-window, and titlebar-accent-preview messages are gone from it (they are shell-bridge calls from trusted pages now, unreachable from agent content).

- Added a defense-in-depth guard so the content view can never navigate to a trusted minds page: an in-page attempt to load a bare backend-origin URL there is blocked (trusted pages only ever render on the chrome surface).

- Opening a workspace from a trusted local page now always routes through the desktop shell so the workspace lands on the (caged) content surface: clicking a workspace row on the Landing page and the redirect when a new workspace finishes creating hand the `/goto/<agent>/` URL to the shell's navigate-content bridge (which also focuses an existing window already on that workspace) instead of navigating the page's own frame. Previously, because these pages now render on the chrome surface, that frame navigation loaded the workspace's (untrusted) agent content into the trusted chrome view. Added the symmetric guard so the chrome view can never navigate to agent content, mirroring the content-view guard above.

- Updated the Electron e2e workspace runner for the surface split: after the create form is submitted (driven on the chrome view), the ready workspace now opens on the separate content view, so the runner waits for that content page to reach the workspace URL (racing the failure view on the chrome view) and drives the dockview / chat / terminal steps on it, rather than on the chrome view that returns to the `/_chrome` wrapper.

- Simplified the desktop auth-cookie handling: sign-in now happens entirely on the trusted default session (login page + sign-in modal on the chrome/modal surfaces), so the old content-partition-to-default `minds_session` watcher was removed; the authenticated cookie is still pushed to the content partition so `/goto` forwarding stays authenticated.

- Hardened and completed the split after a review pass:

  - Fixed a crash where the workspace-crash "Reload" button called a function the split had deleted; both crash-recovery reloads now route through the surface router (the chrome-crash reload no longer strands a local-page window on the empty agent wrapper).

  - The chrome-view guard now also catches server redirects (`will-redirect`) and reroutes agent URLs onto the content surface, so the recovery page's "workspace is healthy" redirect opens the workspace on the caged content view instead of loading agent content into the trusted chrome view; the content-view guard blocks backend-origin redirects too.

  - Agent-controlled notification URLs and the sharing page's workspace link can no longer load agent/foreign content into the privileged chrome view; the recovery page's "Report a problem" button (which had gone dead on the chrome surface) works again.

  - Fixed the workspace-link scheme (the plugin proxy is TLS, so links are now `https`), a Flask request-context guard, three titlebar CSS regressions (in-page modals rendering under the titlebar, the Settings sticky nav, and the post-sign-in sync banner), DevTools / zoom / auth-reload / back-forward now following the visible surface, and backing out of a quit no longer force-reloading a live workspace.

  - The workspace-recovery page now renders under the app titlebar too: it wraps the shared loading card (split out of the `mngr_forward` proxy loader, which is unchanged) in the ChromeShell layout, so the window keeps Home / drag / window controls while a workspace restarts.

  - Reconciled browser (non-Electron) mode: opening a workspace now routes through the agent wrapper `/_chrome?workspace=<agent-id>` (the app titlebar + sidebar wrap the workspace iframe) instead of full-navigating to the bare agent origin and losing the app chrome. The `/_chrome` wrapper's iframe defaults to `about:blank` (no longer `/`, which used to render a second titlebar inside it); the requested workspace id is validated before it's used to build the iframe's `/goto` URL. In browser mode chrome.js is now always full-page navigation (the retired persistent-iframe navigation was removed).

- Local pages now render inside a content card shaped exactly like the workspace surface (4px side/bottom insets below the titlebar, 12px radius), with the accent-tracking background bleeding around it -- so e.g. a workspace's settings page shows the workspace color around its edges just like the workspace itself does, and general pages show the neutral chrome. The card is the page's scroll container (the document itself no longer scrolls on local pages).

- The titlebar no longer flickers white -> accent -> white -> accent when opening a workspace: the `/_chrome` wrapper now seeds the workspace accent inline (resolved server-side and passed as `?accent=<agent-id>`), so the freshly loaded wrapper renderer paints the accent on its first frame instead of coming up neutral until chrome.js is primed.
