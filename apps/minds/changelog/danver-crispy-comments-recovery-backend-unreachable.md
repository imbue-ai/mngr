# Prune a bug-narrative clause from the restart-step-failure docstring

A crispy-comments pass over the `gabriel/recovery-backend-unreachable-inband` branch (PR #304). Trimmed `_report_restart_step_failure`'s docstring so it states why the backend reason is recorded (the first live observation of the outage) and when (before the `RESTART_FAILED` transition), without re-narrating the pre-fix behavior it corrected -- the recovery card opening on a ruled-out verdict and correcting itself a poll later. Comments only; no behavior change.
