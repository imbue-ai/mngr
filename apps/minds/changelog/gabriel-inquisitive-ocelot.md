Minds now reports unexpected errors to Imbue by default for new installs, with an opt-out in Settings.

Automatic error reporting (`report_unexpected_errors`) now defaults on, and the separate "include logs" preference has been removed: a single setting now gates both whether reports are sent and whether their logs/tracebacks are attached, so a report always carries its diagnostics.

The first-launch consent screen is an informational notice: it explains that unexpected errors (with recent logs) are reported to Imbue and offers a single acknowledge button -- no opt-out checkbox there -- and points to Settings for turning it off. The Settings page "Error reporting" section carries a "Report unexpected errors" checkbox that turns automatic reporting off (or back on) for the device; the change takes effect live, without a restart. Both surfaces state that reports include diagnostic details that may identify you (for example, your signed-in account email).

The "report a bug" form no longer has per-report "include logs" or "app diagnostics" checkboxes -- both are always included now (app diagnostics covers the app version, signed-in accounts, the list of workspaces, and system info, but never workspace contents). Details of the workspace the report was opened from (its id, name, host, and provider -- never workspace contents) are also always included now: the old "Details about the current workspace" opt-in was removed, since those details are already reconstructable from the attached logs so leaving it unchecked did not add any real anonymity. Remote access stays opt-in. The backend-down error takeover likewise no longer offers its "Include recent logs" checkbox.

All three error-reporting surfaces (the first-launch notice, the Settings section, and the bug-report form) now state: "Imbue will never look into your workspaces without your consent."

Installs that had previously opted out of error reporting keep their opt-out -- there is no migration that flips reporting back on.

The "report a bug" form's report options have been tidied up: the "Included with every report" and "Optionally include" section headers are gone, the remaining "remote access" opt-in sits on its own, and the two helper notes (what diagnostics are always attached, and that Imbue never looks into your workspaces without consent) now sit together below it.
