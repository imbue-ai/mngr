Trusted local pages in the desktop client now render the app titlebar directly, moving toward a content surface that hosts agent content only.

- Extracted the trusted app shell (the fixed titlebar, the browser-mode floating sidebar, and the accent / `chrome.js` wiring) into a reusable `ChromeShell.jinja` layout component. `Chrome.jinja` (the agent content surface) is now a thin wrapper around it.

- Every trusted local page (Landing, Create, Settings, Accounts, Consent, Welcome, Creating, Destroying, workspace settings, sharing, and the auth-flow pages) now renders that shared titlebar itself, sitting below it in a neutral, full-bleed surface (no accent-tinted content card). `chrome.js` gained a "local page" mode: on a page that is its own main frame (no content iframe), the titlebar's Home / Back / Forward / sidebar navigate the whole page rather than driving a child iframe or the content view.

- The Landing page's "open in new window" and "stop workspace" actions, and the Create page's sign-in prompt, now call the desktop shell bridge directly when available (falling back to the previous relay path during the transition), instead of always routing through the caged content-view relay.
