Cut the minds-v0.4.2 release: bumped the app version to 0.4.2 and pinned `FALLBACK_BRANCH` to the `minds-v0.4.2` default-workspace-template tag, shipping everything landed on main since minds-v0.4.1.

User-selectable release channels, chosen from a new Updates section in Settings: Stable, Beta, and Alpha, with the app updating from the channel you pick.

In-app notifications: a bell button in the titlebar carries an unresolved-count badge, mirrored on the macOS dock and Linux taskbar icon.

Bug reports now carry machine diagnostics. "Report a bug" gains "Include workspace logs" and "Include recent chats", both checked by default.

Recovery no longer blames the machine for device-side faults: failures the forward previously reported as one CONNECT_ERROR are split, so a tunnel this device could not build is attributed to the device rather than the machine.

Sleep and network awareness: a background heartbeat records the windows in which minds was not running, and both the "stopped responding" countdown and the discovery watchdog subtract them, so a laptop that slept is not treated as a stalled machine.

Fixed four macOS windowless dead-ends. Re-opening from the dock, Cmd+N, File > New Window, the dock menu, and relaunching all now produce a window showing real state.

Fixed desktop sign-in with Google on tiers with a dedicated accounts domain: the browser login page now opens on the tier's `accounts_base_url` origin.

Added a `free` plan (1 remote workspace, 5 total workspaces, 25 GB backup storage, no Imbue-Cloud LLM budget, no in-workspace analytics collection) alongside explorer and ally.

Removed the operator CLI surface from `minds`: the `minds env`, `minds pool`, `minds server`, and `minds paid` command groups are deleted outright and moved to a new private `minds-admin` operator CLI.
