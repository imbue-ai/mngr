CI: hardened the `launch-to-msg` end-to-end test against several behaviors introduced on `main`:

- Scroll the chat transcript to the live tail before checking for the agent's reply (the chat virtualizes off-screen rows, so a reply rendered below the fold was absent from the DOM the test read).

- Tear the app down with SIGTERM instead of a graceful close, so the new "Shut down running minds?" quit prompt (a native Electron dialog a headless test cannot click) does not block teardown until the timeout. SIGTERM is routed through the same shutdown chain but flagged headless, matching `just minds-stop`.

- On an empty `/api/create-agent/<id>/status` response, log the raw HTTP code, page URL, and body to diagnose the workspace-creation timeout (status went empty after `WAITING_FOR_READY`).
