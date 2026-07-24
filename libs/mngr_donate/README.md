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
- **Logs.** Each run's full event stream is tee'd to `<host_dir>/donate-logs/<agent>-<ts>.jsonl`,
  and scheduled runs also append to `<host_dir>/donate-logs/schedule.log`, so a run survives the
  agent's auto-destroy for later inspection.

## Running this fork alongside an existing `mngr` install

This branch (`mngr/donate-auth-fix`) carries the `--pass-env CLAUDE_CODE_OAUTH_TOKEN`
fix for the headless donation agent. If you already have `mngr` installed (from
PyPI or another checkout) and want to run *this* fork's `mngr donate` without
disturbing your existing install, run it explicitly with `uv run --project`
pointed at this checkout -- no PATH changes or symlink juggling needed.

```bash
# 1. Install mngr normally (if you haven't) -- the standard PyPI build:
uv tool install imbue-mngr

# 2. Clone this fork and sync its workspace (builds an isolated .venv inside it):
git clone -b mngr/donate-auth-fix <this-repo-url> mngr-donate-auth
cd mngr-donate-auth
uv sync --all-packages

# 3. Run this fork's `mngr donate` explicitly, from anywhere, without touching
#    your global `mngr` (it keeps working for everything else):
uv run --project /path/to/mngr-donate-auth mngr donate --dry-run
uv run --project /path/to/mngr-donate-auth mngr donate
```

Your existing global `mngr` is never modified -- you just never call it for
donate work. To make this ergonomic, add a shell function to your rc file:

```bash
# ~/.zshrc or ~/.bashrc
mngr_donate() {
  uv run --project /path/to/mngr-donate-auth mngr "$@"
}
```

Then:

```bash
mngr_donate donate --dry-run
mngr_donate donate --start        # if you've done the keychain setup above
mngr_donate plugin list          # should list both 'donate' and 'usage'
```

### Why not `mngr donate --start` (launchd)?

`--start` writes a launchd plist whose `ProgramArguments[0]` is the `mngr`
executable in this fork's `.venv/bin/mngr` -- a stable path -- so it keeps
working across reboots. But the plist only injects `PATH` into the launchd
env, **not** `CLAUDE_CODE_OAUTH_TOKEN`. So `--start` only authenticates when
the token is in the macOS **keychain** (the one-time `security
add-generic-password` setup above), which donate reads via `security
find-generic-password`. If you keep the token in an env file (e.g. sourced
from your shell rc) instead of the keychain, launchd ticks would have no
token and fail "Not logged in" -- in that case run a manual loop in a tmux
that sources your env file instead:

```bash
# In a tmux, from inside the fork checkout:
source ~/path/to/your-env-file.sh   # exports CLAUDE_CODE_OAUTH_TOKEN
while true; do
  uv run --project . mngr donate
  sleep 600
done
```

### Verify which `mngr` you're running

```bash
uv run --project /path/to/mngr-donate-auth mngr plugin list   # should list donate
uv run --project /path/to/mngr-donate-auth mngr donate --help
```

If `donate` is missing, you're running the wrong checkout. Confirm with:

```bash
which mngr                          # what a bare `mngr` resolves to
readlink ~/.local/bin/mngr          # where the symlink points (if any)
```
