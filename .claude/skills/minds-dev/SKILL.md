---
name: minds-dev
description: Common minds-app dev tasks. Accepts prompt sub-commands (case-insensitive, keyword match) - "draft" builds a ToDesktop draft, "release" builds + promotes to the latest channel, "create lima agent" / "create agent" drives the backend to create one, "delete agent <id>" deletes via API, "status" reports running backends/VMs/latest build. If the sub-command is ambiguous, ask once which one.
---

# minds-dev

Invoked as `/minds-dev: <prompt>`. Handles common minds-app dev actions - building, releasing, spinning up/tearing down test agents.

The sub-commands below are the common dev loops for iterating on the minds app. Keep responses terse and action-oriented. Only ask the user before doing destructive things (deleting agents, force-pushing, spending build credits). For routine things (`pnpm dist`, running the drive-minds harness) just do them.

## Sub-command: draft / build draft

Trigger: user says "draft", "build draft", "build", "pnpm dist", "todesktop build".

1. Run (background):
   ```
   (cd apps/minds && source ~/.zshrc 2>/dev/null && pnpm dist)
   ```
2. Capture the output to `/tmp/minds-build.log`. Wait for completion notification.
3. From the log, extract the build id + download URL with:
   ```
   grep -oE "26042[a-z0-9]+" /tmp/minds-build.log | sort -u | tail -1
   grep -oE "https://dl\.todesktop[^ ]+arm64" /tmp/minds-build.log | sort -u | tail -1
   ```
4. Report both to the user. Do NOT auto-install unless asked.

## Sub-command: release

Trigger: user says "release", "todesktop release", "promote", "ship".

1. Confirm there isn't a dirty tree (`git status --short` — flag if anything is uncommitted so we don't release code that doesn't match a pushed commit).
2. If the user explicitly said "release this build" / "release <id>", use that id directly. Otherwise run a fresh `pnpm dist` first so we release the current HEAD.
3. Execute:
   ```
   (cd apps/minds && source ~/.zshrc 2>/dev/null && pnpm exec todesktop release <BUILD_ID> --force)
   ```
   or, if we just built and want whatever's newest:
   ```
   (cd apps/minds && source ~/.zshrc 2>/dev/null && pnpm exec todesktop release --latest --force)
   ```
4. Release will upload to the `latest` channel after signing + notarization (both must be configured on the ToDesktop account). Expected errors + their meanings:
   - "Not all platforms were code-signed: Windows ..." — platforms other than Mac don't have signing certs. The user has explicitly said "ignore Windows, we just care about Mac" — ask them if they want to either disable the Windows build on the ToDesktop dashboard, or skip release.
5. Report the release URL / version number on success.

## Sub-command: create lima agent

Trigger: user says "create lima agent", "create agent", "drive create", "make a test agent".

1. Use the harness at `apps/minds/scripts/drive-minds.local.sh`. This is the gitignored HTTP-driver — it auths as the UI and POSTs to `/api/create-agent` + polls status.
2. Prereq: a minds backend must be running on some port. Check with:
   ```
   pgrep -fl "/Applications/minds.app/Contents/Resources/pyproject/.venv/bin/minds" | head
   ```
   - If not running, start the packaged backend directly (bypasses Electron so the one-time-login-code stays fresh for the harness):
     ```
     pkill -9 -f "minds forward" 2>/dev/null
     sleep 3
     nohup env -i HOME=$HOME USER=$USER \
       PATH="$HOME/.minds/lima/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
       ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
       MNGR_HOST_DIR="$HOME/.minds/mngr" MNGR_PREFIX=minds- \
       /Applications/minds.app/Contents/Resources/pyproject/.venv/bin/minds \
       --format jsonl --log-file ~/.minds/logs/minds-events.jsonl forward \
       --host 127.0.0.1 --port 8430 --no-browser \
       > /tmp/minds-backend.log 2>&1 &
     disown
     ```
     Wait ~10s for the "Login URL" line in `~/.minds/logs/minds-events.jsonl`.

3. Run the harness with a fresh baseline:
   ```
   BASELINE=$(grep -c 'Login URL' ~/.minds/logs/minds-events.jsonl)
   # The harness creates TWO agents then deletes the first. If the user
   # only wants one, kill the harness after agent1 reaches DONE.
   BASELINE_LOGIN_COUNT=$BASELINE bash apps/minds/scripts/drive-minds.local.sh > /tmp/drive-minds.out 2>&1 &
   disown
   ```

4. Monitor `/tmp/drive-minds.out` for `status=DONE` / `FAILED`. Report the agent id + whether it hit DONE. The harness talks to `https://github.com/imbue-ai/forever-claude-template` branch `wz/lima-disk-size` by default (origin/main still has the `--memory=4GiB` bug at time of writing). If the user says otherwise, edit the `GIT_BRANCH` line in the harness first.

5. Typical timings (warm caches): ~1:30 CLONING→CREATING→DONE. First-ever run on a fresh Mac: ~5 min (pays VM image download + uv sync).

## Sub-command: delete agent

Trigger: user says "delete agent", "destroy <id>".

1. Need the agent id. If not given, `mngr list` to show options, ask which.
2. Run:
   ```
   MNGR_HOST_DIR=$HOME/.minds/mngr MNGR_PREFIX=minds- \
     /Applications/minds.app/Contents/Resources/pyproject/.venv/bin/mngr destroy <ID> \
     --force --gc --remove-created-branch
   ```
   The `MNGR_HOST_DIR` + `MNGR_PREFIX` env vars are CRITICAL — without them mngr looks in `~/.mngr/` (user's default profile) instead of `~/.minds/mngr/` (minds profile) and silently exits 0 "Agent not found". This was a real bug we hit.
3. Confirm cleanup: `limactl list` should no longer show the VM.

## Sub-command: status

Trigger: "status", "what's running", "state".

Report compactly:
- Running minds processes: `pgrep -fl "/Applications/minds.app/Contents/MacOS/minds$" | head`
- Running Python backends: `pgrep -fl "Contents/Resources/pyproject/.venv/bin/minds forward" | head`
- Backend ports: `lsof -iTCP -sTCP:LISTEN -P | grep python3 | grep 127.0.0.1`
- Lima VMs: `/opt/homebrew/bin/limactl list`
- Latest ToDesktop build: `ls -t /tmp/minds-build*.log | head -1` + grep its id
- Dirty tree: `git status --short`

## Cross-cutting rules

- **Never commit** to this repo or push anywhere unless the user says "commit" / "push".
- **Don't run `pkill -9 -f "/Applications/minds.app"` casually** — it kills the packaged Python processes by pattern-match, which wrecks any in-flight `limactl` destroys and leaves stale state. See `apps/minds/decisions.local.md` for the full story of how this bit us overnight.
- **App launch gotchas** (all documented in `tasks.local.md` + `~/.claude/.../memory/feedback_macos_app_launch_debug.md`):
  - Never re-sign a ToDesktop adhoc bundle after `xattr -cr`.
  - Trashed old copies of the app (same bundle ID) can hijack `open`. Check `lsregister -dump | grep minds.app$` for duplicates.
- **Secrets**: `TODESKTOP_EMAIL` / `TODESKTOP_ACCESS_TOKEN` are in `~/.zshrc` — source it before any `todesktop` command. `ANTHROPIC_API_KEY` is also there if the user has one set.
- **Lima test VMs accumulate** quickly. After a create/delete cycle, `limactl list` + delete any `minds-*-host` we made that the user doesn't actively need.

## Things this skill intentionally does NOT do

- Does not modify code or settings without an explicit ask (`/minds-dev: fix X` is NOT a valid sub-command).
- Does not publish anywhere other than ToDesktop's `latest` channel.
- Does not touch `~/.minds/` data or VMs owned by the user (anything named `selene`, `mngr-test-*`, `mngr-lima-*` was created outside minds-dev sessions — leave alone unless the user points at one).
