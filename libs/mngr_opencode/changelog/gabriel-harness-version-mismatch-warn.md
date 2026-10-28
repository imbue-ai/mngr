New `version_mismatch` setting on the opencode agent type: `ERROR` (the default, unchanged behavior) fails `mngr create` when the installed binary does not report the pinned `version`; `WARN` logs a warning and creates the agent on the installed binary.

Behavior change: the pinned-version check now runs whenever `version` is set, even with `check_installation = false`, which now only controls install-if-missing. The version is now probed with the binary itself rather than the configured launch `command`, so a wrapper command no longer makes the check skip.
