Add a blueprint plan under `blueprint/loading-window-position/` describing
the fix for the startup loading window jumping from the default centered
position to its restored bounds when the backend comes up. The plan
covers reusing the existing `restoreWindowBounds()` helper at the
app-startup site, expected behavior in first-launch, multi-window,
display-gone, and deleted-workspace cases, and the manual verification
scenarios used since this is Electron main-process code with no
automated test harness in the repo.
