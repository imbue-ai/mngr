Added `docs/subsystems-and-recovery.md`, a calibrated subsystem-by-subsystem map of the minds app covering what each subsystem is, how it fails, what the user sees, and the existing recovery mechanisms (including behavior when recovery itself fails).

Also added the underlying audit documents it re-synthesizes: `docs/error-recovery-audit.md` (per-mechanism error-recovery audit) and `docs/subsystem-resilience-report.md` (consolidated resilience report with simplicity-violation findings).

Updated references in these docs from the old `forever-claude-template` name to `default-workspace-template`, matching the repo rename.

Documentation only; no behavior changes.
