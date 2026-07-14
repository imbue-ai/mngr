Added `docs/subsystems-and-recovery.md`, a calibrated subsystem-by-subsystem map of the minds app covering what each subsystem is, how it fails, what the user sees, and the existing recovery mechanisms (including behavior when recovery itself fails).

Also added the underlying audit documents it re-synthesizes: `docs/error-recovery-audit.md` (per-mechanism error-recovery audit) and `docs/subsystem-resilience-report.md` (consolidated resilience report with simplicity-violation findings).

Updated the old `forever-claude-template` references in `docs/subsystems-and-recovery.md` to `default-workspace-template`, matching the repo rename.

Added `docs/subsystem-recovery-updates.md` (ad-hoc response notes to the subsystems report) and `docs/recovery-work-principles.md`, the shared principles (auto-action only on unambiguous evidence; never tear down a rendered view; quiet surfaces still report to Sentry), interface contracts (surfacing pill/page-state shape; environment-signals query API), and self-contained work-unit definitions for the recovery-resilience work spun out of the audit.

Corrected `docs/subsystems-and-recovery.md` against current code: the discovery producer stall threshold is 180s (not 35s), `RECONNECTING` is never emitted over the chrome SSE stream, the recovery verdict tiers reflect the merged PR #2370 / post-#2370 classifier semantics, and the latchkey nudge callout now distinguishes the silent no-match mode from the reported delivery-failure mode.

Documentation only; no behavior changes.
