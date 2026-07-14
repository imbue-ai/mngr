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

## Before you start: authentication (one-time setup)

Do this first. The donation agent is **headless**, and headless Claude authenticates
differently from the Claude app you use interactively -- skipping this step is why a
donate setup "randomly breaks" hours after it worked:

- The desktop app's login popup (`/login`) renews **only the app's own session**. It does
  not refresh the credentials headless `claude` reads.
- A plain web login yields a **short-lived (~8h) access token with no refresh token**. A
  donate setup leaning on it works for a few hours, then every tick fails with
  `Failed to authenticate. API Error: 401 Invalid authentication credentials` -- typically
  overnight.

What donate needs instead is a **long-lived (~1 year) token** minted for exactly this
use case:

```bash
# 1. Mint it (same login popup, but prints a long sk-ant-oat... token in the terminal --
#    copy ALL of it; it usually wraps across lines):
claude setup-token

# 2. Stash it in the macOS keychain (prompts for a "password" -- paste the token there,
#    so it never lands in a file or shell history). Add -U to overwrite an existing entry:
security add-generic-password -s mngr-donate-oauth -a "$USER" -w
```

At each tick, donate looks for that `mngr-donate-oauth` keychain entry and exports it to the
agent as `CLAUDE_CODE_OAUTH_TOKEN` (which takes precedence over the session token). If
`CLAUDE_CODE_OAUTH_TOKEN` is already set in the environment, or the entry doesn't exist, the
environment is inherited unchanged -- so without the stash, donate still runs, but only as
long as your current session token does (and starts 401ing when it lapses).

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

The donation skill (its code **and** prompts) lives in the lab's own upstream git repo --
the single source of truth. `mngr donate` **checks it out** into a host-dir cache
(`<host_dir>/donate-skills/<skill>/`) and points the agent at it, so the lab can revise the
skill without an mngr release:

- `--skill-repo` — the upstream repo (default: the `document-review` skill's GitLab repo).
- `--skill-ref` — the git ref to check out. A **branch** to *track* (each tick adopts the
  latest — good for a fast-moving lab), or a **pinned commit** for a reviewed, reproducible
  version that imbue bumps deliberately.

**Pin the ref for anything unattended.** The donation agent runs with
`--dangerously-skip-permissions`, so whatever code is at `--skill-ref` executes on your
machine. Tracking a branch auto-adopts the lab's changes; pinning a reviewed commit is the
safe default for scheduled runs (bump the ref to adopt updates after review).

## Notes

- **Run it from a trusted git repo.** The donation agent is created from the current
  directory; `--start` bakes that directory (and your `PATH`) into the LaunchAgent.
- **It needs usage data.** Spare capacity is judged from the account-level snapshot, which is
  populated by mngr-managed Claude agents. With none recorded recently, `donate` reports
  "can't tell" and skips rather than guessing.
- **Logs.** Each run writes two siblings under `<host_dir>/donate-logs/`:
  - `<agent>-<ts>.jsonl` — the agent's **assistant text** (reasoning + final summary), tee'd from
    the `mngr create --foreground` parent stdout. (For a `headless_claude` agent the parent stdout
    carries only assistant text deltas — `StreamJsonReader` skips tool calls / tool results — so this
    log is prose-level, not the raw event stream.)
  - `<agent>-<ts>.stream.jsonl` — the agent's **raw stream-json** (tool calls, tool results, the
    skill's outbound HTTP + submissions), captured by live-tailing the agent's own `stdout.jsonl`
    before the agent auto-destroys (which would otherwise delete it). Secret values (OAuth tokens,
    `Authorization: Bearer ...`, `sk-ant-...`) are redacted before writing.
  Scheduled runs also append to `<host_dir>/donate-logs/schedule.log`. Together the two logs let a
  completed run be audited after the agent's auto-destroy, with the raw stream giving per-tool-call
  detail (which papers were leased / submitted) and the assistant-text log giving the agent's own
  summary of the outcome.
