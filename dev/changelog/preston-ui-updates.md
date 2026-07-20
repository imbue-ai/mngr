Added the blueprint plan for the minds titlebar simplification and centered-modals rework (blueprint/titlebar-and-centered-modals/), covering the breadcrumb titlebar with workspace icon-tabs, the home-screen modal launchers, and the per-workspace Connections view that replaces the inbox drawer.

The `just minds-stop` recipe now works on macOS: it walks the process tree with plain `ps` instead of `pstree` (not preinstalled on macOS), uses the portable `ps -o command=` keyword, and recognizes the macOS Electron binary path when locating the main process for the clean-shutdown SIGTERM.
