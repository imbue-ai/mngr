During the alpha, Minds now reports unexpected errors to Imbue by default, and this can no longer be turned off in the app.

Automatic error reporting (`report_unexpected_errors`) now defaults on, and the separate "include logs" preference has been removed: a single setting now gates both whether reports are sent and whether their logs/tracebacks are attached, so a report always carries its diagnostics. The setting is retained internally so reporting can be made opt-out-able again after the alpha.

The first-launch consent screen is now an informational notice: it explains that unexpected errors (with recent logs) are reported to Imbue during the alpha and offers a single acknowledge button -- no opt-in/opt-out checkboxes. The Settings page "Error reporting" section is likewise informational, with no toggles. Both notices state that reports include diagnostic details that may identify you (for example, your signed-in account email).

The "report a bug" form no longer has per-report "include logs" or "app diagnostics" checkboxes -- both are always included now (app diagnostics covers the app version, signed-in accounts, the list of workspaces, and system info, but never workspace contents). Per-workspace details and remote access stay opt-in. The backend-down error takeover likewise no longer offers its "Include recent logs" checkbox.

All three error-reporting surfaces (the first-launch notice, the Settings section, and the bug-report form) now state: "Imbue will never look into your workspaces without your consent."

Installs that had explicitly opted out of error reporting on the previous consent screen are migrated back on once at startup and shown the informational notice again, so reporting is uniformly on during the alpha.
