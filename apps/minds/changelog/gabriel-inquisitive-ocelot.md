During the alpha, Minds now reports unexpected errors to Imbue by default, and this can no longer be turned off in the app.

Automatic error reporting (`report_unexpected_errors`) now defaults on, and the separate "include logs" preference has been removed: a single setting now gates both whether reports are sent and whether their logs/tracebacks are attached, so a report always carries its diagnostics. The setting is retained internally so reporting can be made opt-out-able again after the alpha.

The first-launch consent screen is now an informational notice: it explains that unexpected errors (with recent logs) are reported to Imbue during the alpha and offers a single acknowledge button -- no opt-in/opt-out checkboxes. The Settings page "Error reporting" section is likewise informational, with no toggles. Both notices state that reports include diagnostic details that may identify you (for example, your signed-in account email).

The "report a bug" form no longer has a per-report "include logs" checkbox (logs are always included), and the backend-down error takeover no longer offers its "Include recent logs" checkbox for the same reason.

Installs that had explicitly opted out of error reporting on the previous consent screen are migrated back on once at startup and shown the informational notice again, so reporting is uniformly on during the alpha.
