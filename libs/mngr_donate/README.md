# imbue-mngr-donate

`mngr donate` -- spend spare Claude capacity on a donation skill instead of letting
idle quota expire.

One invocation is a single tick: read the account-level usage snapshot (the same one
`mngr usage` shows), and *if* there's spare capacity (5h window under budget **and** the
week under its pace line), launch a headless Claude agent that runs a donation skill
(default: scientific `document-review`) to completion, then auto-cleans up. When there's
no spare capacity -- or no usage data to judge from -- it does nothing and says so.

This is a separate plugin from `imbue-mngr-usage` on purpose (measuring usage and donating
spare quota are orthogonal), but it depends on it: installing `imbue-mngr-donate` pulls in
`imbue-mngr-usage`, whose snapshot API + `usage` plugin config donate reads at runtime.

## Run a donation

```bash
# From inside a trusted git repo (the agent is sourced from the current dir):
mngr donate                       # one tick: donate now if there's spare capacity
mngr donate --dry-run             # show the decision + numbers, launch nothing
mngr donate --skill my-skill      # run a different skill (default: document-review)
```

## Schedule it (drain spare capacity over time)

A single tick spends at most one skill run's worth of quota. To actually *drain* spare
capacity, schedule it -- the schedule, not any one tick, is what uses up the idle quota:

```bash
mngr donate --start                    # install a launchd LaunchAgent (every 10 min by default)
mngr donate --start --interval-minutes 5
mngr donate --stop                     # remove it
```

`--start`/`--stop` are **macOS-only** and install a **launchd LaunchAgent**
(`com.imbue.mngr.donate` in `~/Library/LaunchAgents/`). launchd -- not cron -- because the
agent must run inside your **login session** to reach the macOS keychain where Claude's
subscription token lives; a cron job runs outside it and every tick fails `Not logged in`.
launchd also catches up after sleep. On other platforms, schedule `mngr donate` yourself.

## Skills: pinned code, dynamic prompts

The donation skill is split so scientists can iterate on experiments without an mngr release:

- **Pinned, reviewed code** (the `client.py`/`sources.py`/`SKILL.md` that talk to the
  coordination server and fetch documents) lives in this repo under
  `.claude/skills/document-review/`. Only imbue-reviewed code ever runs -- important since
  the donation agent runs with `--dangerously-skip-permissions`.
- **Prompts are pulled fresh each run** from the skill's upstream git repo (`--skill-repo`,
  defaulting to the `document-review` skill's repo). `mngr donate` assembles a working copy
  under `<host_dir>/donate-skills/<skill>/` (pinned code + freshly-pulled `prompts/`) and
  points the agent at it. So a reworded rubric or a new prompt file is picked up on the next
  tick; a change to the client *protocol* is a deliberate, reviewed bump of the pinned code.

The pinned `document-review` client is **manual-mode only** -- it has no `ANTHROPIC_API_KEY`
(`run`/`auto`) code path, so a donation run can only ever spend your Claude **subscription**
quota, never direct API billing.

## Notes

- **Run it from a trusted git repo.** The donation agent is created from the current
  directory; `--start` bakes that directory (and your `PATH`) into the LaunchAgent.
- **It needs usage data.** Spare capacity is judged from the account-level snapshot, which is
  populated by mngr-managed Claude agents. With none recorded recently, `donate` reports
  "can't tell" and skips rather than guessing.
- **Logs.** Each run's full event stream is tee'd to `<host_dir>/donate-logs/<agent>-<ts>.jsonl`,
  and scheduled runs also append to `<host_dir>/donate-logs/schedule.log`, so a run survives the
  agent's auto-destroy for later inspection.
