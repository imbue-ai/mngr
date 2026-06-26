CI: hardened the `launch-to-msg` end-to-end test against several behaviors introduced on `main`:

- Treat the creating page auto-navigating to the workspace as creation success. On `DONE`, `creating.js` redirects the page to the workspace origin, so the `/api/create-agent/<id>/status` poll then hit the workspace (HTML, not JSON) and read an empty status -- the test never observed `DONE` and timed out after 900s. It now recognizes the workspace URL as done (and skips its own redirect when already there).

- Tear the app down with SIGTERM (then a bounded SIGKILL fallback) instead of a graceful close, so the new "Shut down running minds?" quit prompt -- a native Electron dialog a headless test cannot click -- does not block teardown until the test timeout. SIGTERM is routed through the same shutdown chain but flagged headless, matching `just minds-stop`.

- Scroll the chat transcript to the live tail before checking for the agent's reply (the chat virtualizes off-screen rows, so a reply rendered below the fold was absent from the DOM the test read).
