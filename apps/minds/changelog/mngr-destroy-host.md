- `apps/minds`: **fix a silent-orphan bug where a failed host teardown could
  leave an invisible, still-billing host.** A destroying agent's status is
  now derived from the destroy's own recorded exit code instead of being
  inferred from the lagging `mngr observe` discovery cache. The detached
  `bash` wrapper runs under `set -o pipefail` and atomically records `mngr
  destroy`'s exit code to `<data_dir>/destroying/<agent_id>/result` on
  completion; `read_destroying` reads that recorded outcome first (exit 0
  -> `done`, non-zero -> `failed`), falling back to PID liveness only while
  no result has been recorded (`running`), and treating a wrapper that died
  before recording as `failed`. Previously, on a fresh app open the
  discovery cache is empty for a few seconds, so a genuinely-failed destroy
  read pid-dead + agent-absent -> `done` -> the record was deleted and the
  live host was orphaned; now a failed destroy reads `failed` from its own
  exit code regardless of the cache. This also eliminates the ~1-second
  jitter where a clean destroy briefly read "failed" while discovery caught
  up (a successful destroy now reads `done` from its recorded `0`).
- `apps/minds`: the landing page now renders a destroy record's row even when
  discovery does not list its agent (the displayed rows are the union of
  discovered workspace agents and agents with a live destroy record). This
  closes the rendering half of the silent-orphan bug: a `failed` destroy
  whose agent the resolver has dropped is still surfaced as a "Destroy
  failed" row linking to its log/retry page, instead of disappearing from
  the UI while its host keeps billing. Holds even when no agents are
  discovered at all (the destroy rows replace the empty/"Discovering…"
  state).
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
