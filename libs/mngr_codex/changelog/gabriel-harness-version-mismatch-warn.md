New `version_mismatch` setting on the codex agent type: `ERROR` (the default, unchanged behavior) fails `mngr create` when the installed binary does not report the pinned `version`; `WARN` logs a warning and creates the agent on the installed binary.

codex already ran the check whenever `version` was set, so only the new setting changes for it. The version is now probed with the binary itself rather than the configured launch `command`, so a wrapper command no longer makes the check skip.
