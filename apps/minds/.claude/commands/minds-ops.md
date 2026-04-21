---
description: Common minds-app dev tasks. draft | release | create agent | delete agent <id> | status
argument-hint: "draft | release | create agent | delete agent <id> | status"
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, WebSearch, WebFetch
---

# /minds-ops

Handles common minds-app dev actions - building, releasing, spinning up/tearing down test agents.

User arguments: `$ARGUMENTS`

Parse the argument to pick ONE sub-command (case-insensitive, keyword match). If ambiguous, ask once which one.

Keep responses terse and action-oriented. Only ask the user before doing destructive things (deleting agents, force-pushing, spending build credits). For routine things (`pnpm dist`, running the drive-minds harness) just do them.

## Sub-command: draft / build draft

Trigger: "draft", "build draft", "build", "pnpm dist", "todesktop build".

1. Run in background:
   ```
   (cd apps/minds && source ~/.zshrc 2>/dev/null && pnpm dist) 2>&1 | tee /tmp/minds-build.log
   ```
2. Wait for completion notification.
3. From the log, extract the build id + download URL:
   ```
   grep -oE "26042[a-z0-9]+" /tmp/minds-build.log | sort -u | tail -1
   grep -oE "https://dl\.todesktop[^ ]+arm64" /tmp/minds-build.log | sort -u | tail -1
   ```
4. Report both. Do NOT auto-install unless asked.

## Sub-command: release

Trigger: "release", "todesktop release", "promote", "ship".

1. `git status --short` — flag if tree is dirty; user probably doesn't want to release code that doesn't match a pushed commit.

2. **BUMP THE VERSION.** This is mandatory — ToDesktop's auto-updater compares installed vs channel version via semver, so re-releasing the same version number produces no update prompt for existing users. Silent no-op that wastes a release cycle.
   - Read current: `grep '"version"' apps/minds/package.json`
   - Pick: patch bump (`0.1.0` → `0.1.1`) for internal / dogfood changes, minor (`0.1.1` → `0.2.0`) for user-visible behavior change, major (`0.2.0` → `1.0.0`) for a public cut.
   - When unsure, default to patch bump and tell the user what you chose.
   - Edit `apps/minds/package.json` `"version"` field, then commit: `git add apps/minds/package.json && git commit -m "Bump minds to v<new>"`.
   - Only skip the bump if the user explicitly says "re-release the same version" — and warn them the auto-updater will not fire.

3. If the user said "release this build" / "release <id>", use that id. Otherwise `pnpm dist` first so we release HEAD (with the bumped version).

4. Execute:
   ```
   (cd apps/minds && source ~/.zshrc 2>/dev/null && pnpm exec todesktop release <BUILD_ID> --force)
   ```
   or:
   ```
   (cd apps/minds && source ~/.zshrc 2>/dev/null && pnpm exec todesktop release --latest --force)
   ```

5. Expected error: "Not all platforms were code-signed: Windows ..." — user has explicitly said "ignore Windows, we just care about Mac". Ask if they want Windows disabled on the ToDesktop dashboard, or skip release.

6. Report the release URL + the version number just released. Mention that existing installs will see the update on their next autoCheckInterval tick (default ~1 hour), or immediately via File > Check for Updates... in the app.

## Sub-command: create lima agent

Trigger: "create lima agent", "create agent", "drive create", "make a test agent".

1. Backend prereq: `pgrep -fl "/Applications/minds.app/Contents/Resources/pyproject/.venv/bin/minds"`. If not running, start the packaged backend directly (bypasses Electron so the one-time login code stays fresh for the harness):
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

2. Run the harness with a fresh baseline:
   ```
   BASELINE=$(grep -c 'Login URL' ~/.minds/logs/minds-events.jsonl)
   BASELINE_LOGIN_COUNT=$BASELINE bash apps/minds/scripts/drive-minds.local.sh > /tmp/drive-minds.out 2>&1 &
   disown
   ```
   The harness (`drive-minds.local.sh`) is gitignored; if it's missing, create it from memory — it auths via the Login URL, POSTs to `/api/create-agent` with `branch=wz/lima-disk-size`, polls `/api/create-agent/<id>/status` until DONE or FAILED.

3. Monitor `/tmp/drive-minds.out` for `status=DONE` / `FAILED`. Report the agent id + final state.

4. Default branch: `wz/lima-disk-size` (origin/main of forever-claude-template still has the `--memory=4GiB` bug at time of writing). If the user wants main, edit `GIT_BRANCH` in the harness first.

5. Typical timings (warm caches): ~1:30 CLONING→CREATING→DONE. Fresh Mac first run: ~5 min.

## Sub-command: delete agent

Trigger: "delete agent", "destroy <id>".

1. Need the agent id. If not given, run `MNGR_HOST_DIR=$HOME/.minds/mngr MNGR_PREFIX=minds- /Applications/minds.app/Contents/Resources/pyproject/.venv/bin/mngr list` and ask which.
2. Run with the critical env vars:
   ```
   MNGR_HOST_DIR=$HOME/.minds/mngr MNGR_PREFIX=minds- \
     /Applications/minds.app/Contents/Resources/pyproject/.venv/bin/mngr destroy <ID> \
     --force --gc --remove-created-branch
   ```
   Without `MNGR_HOST_DIR` + `MNGR_PREFIX`, mngr looks in `~/.mngr/` (default profile) instead of `~/.minds/mngr/` and silently exits 0 "Agent not found". Known footgun.
3. Confirm cleanup: `/opt/homebrew/bin/limactl list` should no longer show the VM.

## Sub-command: status

Trigger: "status", "what's running", "state".

Report compactly:
- Running minds electron: `pgrep -fl "/Applications/minds.app/Contents/MacOS/minds$" | head`
- Running Python backends: `pgrep -fl "Contents/Resources/pyproject/.venv/bin/minds forward" | head`
- Backend ports: `lsof -iTCP -sTCP:LISTEN -P | grep python3 | grep 127.0.0.1`
- Lima VMs: `/opt/homebrew/bin/limactl list`
- Latest ToDesktop build: `ls -t /tmp/minds-build*.log | head -1` + extract id
- Dirty tree: `git status --short`

## Cross-cutting rules

- **Never commit** or push anywhere unless the user says "commit" / "push".
- **Don't run `pkill -9 -f "/Applications/minds.app"` casually** — pattern-matches packaged Python and wrecks in-flight `limactl` destroys. Use specific PIDs instead.
- **App launch gotchas**:
  - Never re-sign a ToDesktop adhoc bundle after `xattr -cr`.
  - Trashed old copies (same bundle ID) can hijack `open`. Check `lsregister -dump | grep minds.app$` for duplicates.
- **Secrets**: `TODESKTOP_EMAIL` / `TODESKTOP_ACCESS_TOKEN` + `ANTHROPIC_API_KEY` are in `~/.zshrc` — source it before any `todesktop` command.
- **Don't touch** user-owned VMs (anything named `selene`, `mngr-test-*`, `mngr-lima-*`, non-`minds-*` lima VMs) unless the user points at one.

## NOT in scope for this command

- Code modifications (use a regular prompt, not `/minds-ops`).
- Publishing anywhere other than ToDesktop's `latest` channel.
- Anything not listed above — ask the user what they meant.
