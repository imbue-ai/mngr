Cut the minds-v0.4.3 release: bumped the app version to 0.4.3 and pinned `FALLBACK_BRANCH` to the `minds-v0.4.3` default-workspace-template tag, shipping everything landed on main since minds-v0.4.2.

Machine updates: minds now compares each machine's template version against the version the build supports and surfaces the result everywhere (a badge on the machines list, a notice band, Settings -> Updates, and an update modal), with "Update all now" and "Schedule all updates" once more than one machine is behind. Machines older than minds-v0.3.10 are badged "Recreate to update" instead of being offered in-place migration machinery.

Titlebar unification: the five popup icons (Permissions, Machine settings, Share machine, notifications, bug report) now behave as one strip -- only one surface open at a time, one click between any two, and Back no longer walks through every popup visited.

Permission-request verdicts now pair with the right card by request id, so resolving two pending requests out of order can no longer swap their verdicts.

Right-clicking now opens a standard clipboard context menu (Cut/Copy/Paste/Select All in editable fields, Copy on text selections).

Stopped cloud workspaces are no longer dead-end "on <device>" tiles: cloud records are badged "Imbue Cloud", and only machines hosted by another install are badged with the device name.

Locality is decided by the address a workspace connects to, not its provider type, so a Docker daemon on another machine is treated as remote; held restarts are released one machine at a time when the network returns, and the connectivity check's worst case drops from ~16.5s to ~7.5s.

The packaged app now bundles the opencode and antigravity mngr plugins, ships a `uv-shims/install_name_tool` so first launch on a Mac without Xcode Command Line Tools no longer raises the developer-tools alert, and uploads rotated mngr CLI/latchkey event logs in bug reports.

Workspace destroy targets the host directly (`mngr destroy @<host_id>.<provider> --force`) instead of piping a discovery-derived agent listing, closing the partial-destroy failure mode.

Latchkey is bumped to 3.9.0.
