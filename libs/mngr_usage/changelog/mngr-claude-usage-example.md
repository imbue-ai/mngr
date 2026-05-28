Added a "cron automation recipes" doc (`docs/cron_recipes.md`), linked from the
README, with three worked examples of driving `mngr` from `cron` using check
mode (`mngr usage --format json`) rather than the blocking `mngr usage wait`:

- Soak up an about-to-expire 5h window: relaunch a known agent when the 5h
  window is near its end with budget left, and schedule a `mngr stop` for the
  window boundary. The weekly guard is a *pace* check with a 30% margin on the
  remaining week -- `(100 - used%) > 0.70 * (100 - elapsed%)` -- so it stays
  strict early in the week (won't spend at 70% used on a Monday) but converges,
  as the week ends, to "launch if any capacity is left," rather than using a
  flat ceiling.
- Warm a fresh window: detect that the last recorded 5h window has elapsed
  (`five_hour.resets_at < now`) and fire a throwaway headless `claude -p`
  prompt to open/prime the new window, guarded by a marker file so it warms at
  most once per boundary.
- Dispatch a queue of task files: name each agent after its task file and cap
  concurrency by a shared `queue=tasks` label (counting `RUNNING`/`WAITING`
  pool members via `mngr list`).

All scripts guard against nudging an already-running agent.
