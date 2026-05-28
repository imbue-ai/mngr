Added a "cron automation recipes" doc (`docs/cron_recipes.md`), linked from the
README, with three worked examples of driving `mngr` from `cron` using check
mode (`mngr usage --format json`) rather than the blocking `mngr usage wait`:

- Use up an about-to-expire 5h window: one cron job owns a dedicated agent's
  whole lifecycle, branching on its state each tick -- it starts the agent during
  the tail of an open 5h window when there's budget left and the week is on pace,
  and stops it once the window rolls over or the week falls off pace (so no `at`
  / scheduled stop is needed, and running one tick into the fresh window warms
  it). The weekly guard is a *pace* check with a tapering safety margin --
  `used% < elapsed% * (1 - 0.30 * (100 - elapsed%) / 100)` -- which leaves
  headroom early in the rolling 7-day cycle (requires used% < ~10 around ~1 day
  in) but converges, as the cycle ends, to "use it if any capacity is left." It
  acts on age-stale readings (a quiet account is when there's leftover budget)
  but skips a window that has already reset.
- Warm a fresh 5h window early: detect that the last recorded window has elapsed
  (`five_hour.resets_at < now`) and nudge a dedicated warming agent to fire one
  prompt and open the next window. The 5h window starts on your first prompt, so
  pre-starting it makes it reset partway through your work rather than a full 5h
  later. The agent is reused across boundaries (create once, then
  start/message/stop) and never destroyed -- a stopped agent keeps its usage
  reading, so the trigger self-clears and no marker file is needed.
- Dispatch a queue of task files: name each agent after its task file, launch it
  from the project repo, and cap concurrency by a shared `queue=tasks` label
  (counting `RUNNING`/`WAITING` pool members via `mngr list`). The same label
  marks the pool's own agents, so it can retire its finished (`WAITING`) members
  without touching agents you started yourself.
