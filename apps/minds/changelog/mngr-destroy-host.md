- `apps/minds`: derive a destroying agent's status from the destroy's own
  recorded exit code instead of inferring it from discovery host state. This
  builds on #2149 (whole-host teardown + `list_active_workspace_ids()` +
  disassociate-at-DONE, all kept) and replaces only the status derivation: the
  detached `bash` wrapper now runs under `set -o pipefail` and atomically
  records `mngr destroy`'s exit code to `<data_dir>/destroying/<agent_id>/result`
  on completion, and `read_destroying` reads that recorded outcome first (exit 0
  -> `done`, non-zero -> `failed`), falling back to PID liveness only while no
  result has been recorded (`running`), and treating a wrapper that died before
  recording as `failed`. Because the verdict comes from the destroy itself
  rather than the lagging `mngr observe` cache, it closes three residual gaps
  #2149's discovery-based check still had: a genuinely-failed destroy is no
  longer mistaken for `done` during the pre-discovery window on app reopen
  (which would disassociate + delete the record and orphan a still-billing
  host); a successful destroy no longer flickers to `failed` while discovery
  catches up; and a recycled PID can no longer pin a finished destroy at
  `running` forever (the wrapper's `psutil` `create_time` is recorded to
  `process_start` and checked on read).
- `apps/minds`: the landing page now renders a destroy record's row even when
  discovery does not list its agent (the displayed rows are the union of active
  workspace agents and agents with a live destroy record), so a `failed`
  destroy whose host the resolver has dropped is still surfaced as a "Destroy
  failed" row linking to its log/retry page instead of disappearing while its
  host keeps billing. Holds even when no agents are discovered at all.
- `apps/minds`: `GET /api/destroying/<id>/status` now returns the recorded
  `exit_code` (null until the destroy finishes) in place of the
  `is_host_still_active` field.
