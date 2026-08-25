# Document the restore download lock in the stop/start operations guide

`docs/deploy/workspace-stop-start.md` now describes the per-box download lock restores queue on (`STAGE=waiting-for-lock`, the bounded wait and its `transition_error`), how to find a leaked holder, and the safe remediation for a box wedged by a pre-fix restore.
