Added a "cron automation recipes" doc (`docs/cron_recipes.md`), linked from the
README, with three worked examples of driving `mngr` from `cron` using check
mode (`mngr usage --format json`) rather than the blocking `mngr usage wait`:

- Use up an about-to-expire 5h window: relaunch a known agent when the 5h
  window is near its end with budget left, and schedule a `mngr stop` for the
  window boundary. The weekly guard is a *pace* check with a tapering safety
  margin -- `used% < elapsed% * (1 - 0.30 * (100 - elapsed%) / 100)` -- so it
  leaves headroom early in the rolling 7-day cycle (requires used% < ~10 around
  ~1 day in, staying out of the user's way) but, as the margin shrinks toward the
  cycle's end, converges to "launch if any capacity is left."
- Warm a fresh 5h window early: detect that the last recorded window has elapsed
  (`five_hour.resets_at < now`) and fire a throwaway headless `claude -p` prompt
  to start the next window. The 5h window starts on your first prompt, so
  pre-starting it makes it reset partway through your work rather than a full 5h
  later. Guarded by a marker file so it fires at most once per boundary.
- Dispatch a queue of task files: name each agent after its task file and cap
  concurrency by a shared `queue=tasks` label (counting `RUNNING`/`WAITING`
  pool members via `mngr list`).

All scripts guard against nudging an already-running agent.
