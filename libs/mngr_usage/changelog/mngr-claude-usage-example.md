Added a "cron automation recipes" doc (`docs/cron_recipes.md`), linked from the
README, with worked examples of driving `mngr` from `cron` using check mode
(`mngr usage --format json`) rather than the blocking `mngr usage wait`, plus a
shared `spare-capacity.sh` helper (exit 0 when the 5h window still has budget and
the week is under pace):

- Use up an about-to-expire 5h window: one cron job owns a dedicated agent's whole
  lifecycle, starting it in the tail of an open 5h window when there's spare
  capacity and stopping it once the window rolls over or the week falls off pace.
- Warm a fresh 5h window early: when the last recorded window has elapsed, nudge a
  dedicated warming agent to fire one prompt and open the next window so it resets
  partway through your work rather than a full 5h later.
- Dispatch a queue of task files: launch an agent per task file from the project
  repo, only while there's spare capacity, capped by a shared `queue=live` label;
  finished agents are stopped and relabeled `queue=in-review` for later review.
