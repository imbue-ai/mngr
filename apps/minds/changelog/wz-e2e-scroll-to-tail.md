CI: hardened the `launch-to-msg` end-to-end test against several behaviors introduced on `main`:

- Treat the creating page auto-navigating to the workspace as creation success. On `DONE`, `creating.js` redirects the page to the workspace origin, so the `/api/create-agent/<id>/status` poll then hit the workspace (HTML, not JSON) and read an empty status -- the test never observed `DONE` and timed out after 900s. It now recognizes the workspace URL as done (and skips its own redirect when already there).

- Tear the app down with SIGTERM (then a bounded SIGKILL fallback) instead of a graceful close, so the new "Shut down running minds?" quit prompt -- a native Electron dialog a headless test cannot click -- does not block teardown until the test timeout. SIGTERM is routed through the same shutdown chain but flagged headless, matching `just minds-stop`.

- Scroll the chat transcript to the live tail before checking for the agent's reply (the chat virtualizes off-screen rows, so a reply rendered below the fold was absent from the DOM the test read).

- Made the self-hosted mac-runner reset (`mac-runner-reset.sh`) best-effort *and* fail-loud on a dirty runner: dropped `set -e` so a single failing cleanup step (e.g. a `df`/`find` pipe) can no longer abort the script and skip the remaining Lima-VM / disk cleanup; then, after every step runs, the script verifies the end state (no surviving `minds-e2e` Lima VM, no `~/.minds`, app removed) and exits non-zero if the runner is not actually clean. The post-test cleanup step no longer wraps it in `|| true`, so a leaked VM that would otherwise silently rot this non-ephemeral runner now fails the job. The optional app-install block still fails loud so a run never proceeds against a stale app.

- Dismiss the new post-login "Help improve Minds" error-reporting consent screen (`Consent.jinja`) before creating workspaces, so the home-page "both tiles render" assertion sees the home page rather than the consent screen.

- Fixed the `macos_launch` smoke test's worker-exit hang (and the resulting red job despite a passing assertion): the app spawns detached helpers (the minds python backend, a `mngr latchkey forward` supervisor in its own process group, the crashpad handler) that outlive the main process and keep the Playwright worker's inherited stdio sockets open, so the worker was force-killed after 300s. Teardown now reaps those processes by command pattern and unrefs stdout/stderr so the worker exits cleanly.
