Desktop app auto-update and developer-tooling fixes (extracted from the larger minds onboarding work for standalone review).

- Auto-update: packaged builds now prompt to install a downloaded update. ToDesktop's runtime defaults `showInstallAndRestartPrompt` to `"never"`, so users saw "downloading in the background..." and were never prompted again; it is now set to `"always"`. ToDesktop is only initialized in packaged builds -- in dev its constructor threw on macOS (Squirrel is not linked in the unsigned binary), so dev launches now skip it.
- Added a `File > Check for Updates...` menu item that triggers a check and reports the result (update found / up to date / unavailable in draft builds / error).
- Added a `View` menu with `Toggle Developer Tools` (Alt+Cmd+I), zoom controls, and fullscreen. The default Electron DevTools shortcut crashed because the app uses `BaseWindow` + `WebContentsView` rather than a `BrowserWindow`.
- `MINDS_OPEN_DEVTOOLS=1` auto-opens detached DevTools on the content view at launch.
- The content `WebContentsView` now loads `preload.js`.
- Startup env-setup failures are now logged to the console in addition to being shown in the error window.
