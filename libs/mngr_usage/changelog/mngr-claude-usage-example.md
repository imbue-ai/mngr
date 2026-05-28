Added a "cron automation recipes" doc (`docs/cron_recipes.md`), linked from the
README, with three worked examples of driving `mngr` from `cron` using check
mode (`mngr usage --format json`) rather than the blocking `mngr usage wait`:

- Soak up an about-to-expire 5h window: relaunch a known agent when the 5h
  window is near its end with budget left, and schedule a `mngr stop` for the
  window boundary. The weekly guard is a *pace* check (`seven_day.used_percentage
  < seven_day.elapsed_percentage`) rather than a flat ceiling, so it won't spend
  when you're already ahead of the sustainable weekly burn.
- Warm a fresh window: detect that the last recorded 5h window has elapsed
  (`five_hour.resets_at < now`) and touch an agent to open/prime the new window.
- Dispatch a queue of task files: name each agent after its task file and cap
  concurrency by a shared `queue=tasks` label (counting `RUNNING`/`WAITING`
  pool members via `mngr list`).

All scripts guard against nudging an already-running agent.
