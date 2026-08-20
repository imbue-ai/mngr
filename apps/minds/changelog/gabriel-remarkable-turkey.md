Fixed four ways the macOS desktop app could dead-end after every window was closed (the keep-the-app-running behavior added in the previous release).

Re-opening a window from the dock, `Cmd+N`, `File > New Window`, the dock menu, or by launching the app again now always produces a window, showing the app's real state: the home page when the backend is serving, the loading screen while it is still starting, and the error screen with its **Retry** button when startup failed or the backend died. Previously, an app whose backend had failed to start could never open a window again and sat inert in the dock until `Cmd+Q`.

A backend that crashes while no window is open is now reported and recoverable. Before, nothing was logged or shown, and the next window opened onto the dead port with a Reload button that only re-loaded it.

Closing the window while the app is still starting no longer strands it: the backend finishes coming up and signs you in as usual (previously every window opened afterwards landed on a sign-in page asking for a one-time link that a packaged app never prints). The landing that launch owed you -- your restored session, or the welcome / error-reporting screens -- is applied to the next window you open, instead of being skipped or popping windows up unprompted. It is worked out afresh at that point, so a session restored much later reflects the machines you actually have.

A `minds://` deeplink that arrives while no window is open now opens one instead of being silently dropped. This restores the browser sign-in flow's "Open app" link.
