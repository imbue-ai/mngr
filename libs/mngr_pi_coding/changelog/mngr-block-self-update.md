Added a `version` field to the pi agent type that pins the installed pi CLI: installation runs `npm install -g @earendil-works/pi-coding-agent@<version>` and provisioning verifies the installed pi matches, erroring on a mismatch.

Added an `update_policy` field that governs pi's startup version check. `NEVER` sets `PI_SKIP_VERSION_CHECK=1` so pi does not phone home to compare against the latest release; `AUTO` leaves the check enabled; `ASK` behaves like `AUTO`. When unset, it defaults to `NEVER` (check disabled) -- set `AUTO` to re-enable it.
