The hourly pool-host cleanup cron now also reconciles each bare-metal box's lima slices against the pool database, scoped to this deployment's own environment (via `MINDS_ENV_NAME`).

A slice stamped for this env that is present on a box but has no database row is reaped (the periodic analogue of the bake-time orphan reaper); a database row whose VM has vanished is logged as a divergence for manual handling. Other environments' slices and legacy un-stamped slices are never touched, so the reconcile is safe on a box shared by multiple dev environments.
