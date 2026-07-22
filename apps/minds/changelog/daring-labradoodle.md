Polished the Google / GitHub sign-in flow on the login page so the OAuth round-trip feels responsive instead of frozen:

- Clicking a provider button now gives immediate feedback: that provider's logo is replaced by a spinner and both provider buttons fade slightly while the browser round-trip runs.

- The status ("blue box") message is now staged and anchored to real progress rather than a single frozen line: "Opening your browser..." on click, "Waiting for you to finish signing in with Google/GitHub..." once the flow starts, and "Finishing up..." while your account is set up.

- When sign-in completes in the external browser, the Minds window promptly raises itself to the front (only if it wasn't already focused) so you land back in the app instead of having to switch back manually. It comes forward as soon as sign-in lands -- while the account finishes wiring up in the background -- and the login page polls more frequently so there's minimal lag.

- OAuth failures, timeouts, and lost flows now surface in the same in-page error box (and cleanly reset the buttons) instead of interrupting with a browser `alert()` popup.

- Removed the "Continue with GitHub" button from the sign-in / sign-up page, since GitHub OAuth is not enabled in the current deployment; Google is now the only third-party sign-in option. The underlying GitHub provider support is left in place so the button can return once credentials are configured.
