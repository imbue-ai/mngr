The app is now called Mind.

`productName` is `Mind`, so the bundle, its helpers, the macOS menu and About panel, and the published installer all carry the new name. The window title, the launch and crash screens, and the backend start-up status say "Mind" too.

The first launch after the change removes the application-support, logs, and cache directories the previous name owned, instead of stranding them. They hold crash-reporter scratch, so nothing is moved into the new locations -- which the app has already opened by then. The updater cache is untouched: its name comes from the package name rather than the product name, so it did not move, and it can hold a staged update.

The quit dialogs now say "workspace". Quitting with workspaces still running used to say "1 local mind is still running", naming them with a word that appears nowhere else in the app, and the dialog lists workspace names.

The frontend test helper `settle()` now waits a timer turn instead of three microtask hops. A real `Response.json()` takes eight hops to land, so a page that owns its own model could be asserted on before its payload arrived; three tests were failing on it.

More places say Mind: the first onboarding screen's caption, the credential-permission dialogs, and the WSL installer's desktop shortcut.
