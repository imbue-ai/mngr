- `apps/minds`: derive a destroying agent's status from the destroy's own
  recorded exit code instead of inferring it from the lagging `mngr
  observe` discovery cache. The detached `bash` wrapper now runs under
  `set -o pipefail` and atomically records `mngr destroy`'s exit code to
  `<data_dir>/destroying/<agent_id>/result` on completion; `read_destroying`
  reads that recorded outcome first (exit 0 -> `done`, non-zero ->
  `failed`), falling back to PID liveness only while no result has been
  recorded (`running`), and treating a wrapper that died before recording
  as `failed`. This makes the landing page show "Destroying…" steadily
  while a destroy is in flight and only surface "Destroy failed" on a
  genuine failure -- eliminating the ~1-second jitter where a clean
  destroy briefly read "failed" while discovery caught up, and the
  closed-app reopen failure modes where a genuinely-failed destroy could
  be silently finalized as "done" (orphaning a still-billing host) or a
  recycled PID could pin a finished destroy at "Destroying…" forever.
- `apps/minds`: make the destroy wrapper's PID-liveness check reuse-safe by
  recording the wrapper's `create_time` to a `process_start` file and
  rejecting a PID whose live process no longer matches it.
- `apps/minds`: the landing page now keeps the "Destroying…" marker on a
  succeeded destroy until discovery actually drops the agent, then deletes
  the record -- so the row never flickers back to a normal clickable state
  mid-teardown. The resolver is consulted only for this finalize timing,
  never to decide `done` vs `failed`.
- `apps/minds`: `GET /api/destroying/<id>/status` now returns the recorded
  `exit_code` (null until the destroy finishes) in place of the removed
  `agent_in_resolver` field.
