Added a "cron automation recipes" doc (`docs/cron_recipes.md`), linked from the
README, with worked examples of driving `mngr` from `cron` using check mode
(`mngr usage --format json`) rather than the blocking `mngr usage wait`, plus a
shared `spare-capacity.sh` helper (exit 0 when the 5h window still has budget and
the week is under pace):

- Use up an about-to-expire 5h window: one cron job owns a dedicated agent's whole
  lifecycle, branching on its state each tick -- it starts the agent during the
  tail of an open 5h window when there's budget left and the week is on pace, and
  stops it once the window rolls over or the week falls off pace (so no separate
  scheduled stop is needed, and running one tick into the fresh window warms it).
  The weekly guard is a *pace* check: START needs weekly used% below a tapering
  safety margin -- `used% < elapsed% * (1 - 0.30 * (100 - elapsed%) / 100)`, which
  leaves headroom early in the rolling 7-day cycle but converges, as the cycle
  ends, to "use it if any capacity is left" -- while STOP uses a looser line
  (`used% = elapsed%`), the gap between them being a hysteresis band so it can't
  thrash. It acts on age-stale readings but skips a window that has already reset.
- Warm a fresh 5h window early: detect that the last recorded window has elapsed
  (`five_hour.resets_at < now`) and nudge a dedicated warming agent to fire one
  prompt and open the next window. The 5h window starts on your first prompt, so
  pre-starting it makes it reset partway through your work rather than a full 5h
  later. The agent is reused across boundaries (create once, then
  start/message/stop, via `mngr wait ... WAITING`) and never destroyed -- a stopped
  agent keeps its usage reading, so the trigger self-clears and no marker is needed.
- Dispatch a queue of task files: name each agent after its task file, launch it
  from the project repo, and -- only while `spare-capacity.sh` reports headroom --
  cap concurrency by a shared `queue=live` label. Finished (`WAITING`) members are
  stopped and relabeled `queue=in-review`, freeing a pool slot while parking them
  for you to restart and review (`mngr list --label queue=in-review`).
