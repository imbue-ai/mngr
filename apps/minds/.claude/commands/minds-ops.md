---
description: Common minds-app dev tasks. launch | draft | release | create agent | delete agent <id> | status | interact <prompt>
argument-hint: "launch | draft | release | create agent | delete agent <id> | status | interact <prompt>"
allowed-tools: Bash, Read, Write, Edit, Grep, Glob, WebSearch, WebFetch
---

# /minds-ops

Handles common minds-app dev actions - building, releasing, spinning up/tearing down test agents.

User arguments: `$ARGUMENTS`

Parse the argument to pick ONE sub-command (case-insensitive, keyword match). If ambiguous, ask once which one.

Keep responses terse and action-oriented. Only ask the user before doing destructive things (deleting agents, force-pushing, spending build credits). For routine things (`pnpm dist`, running the drive-minds harness) just do them.

## Sub-command: launch

Trigger: "launch", "run", "dev", "local", "pnpm start".

Launch the Electron app from local source (no packaging). Code changes in `apps/minds/electron/` + `imbue/minds/desktop_client/templates.py` etc. are picked up by restarting; wheel changes need a rebuild.

1. Kill any already-running local dev instance so the new one doesn't trip the Electron single-instance lock:
   ```
   pkill -9 -f "electron .*apps/minds" 2>/dev/null
   pkill -9 -f "uv run --package minds minds" 2>/dev/null
   sleep 2
   ```
2. Start. **Must** `unset ELECTRON_RUN_AS_NODE` first — it's set in the user's shell env for other tooling, and if inherited, Electron boots in `node_init` mode where `require('electron')` returns a string path and main.js crashes at `app.isPackaged`:
   ```
   (cd apps/minds && unset ELECTRON_RUN_AS_NODE && source ~/.zshrc 2>/dev/null && unset ELECTRON_RUN_AS_NODE && pnpm start) > /tmp/minds-local.log 2>&1 &
   disown
   ```
   (unset both before *and* after sourcing zshrc in case zshrc re-sets it.)
3. Wait ~10 s for the backend to bind and the window to appear, then report:
   - PID of the Electron main: `pgrep -f "electron.*apps/minds" | head -1`
   - Backend port: extract from `/tmp/minds-local.log` (look for "Backend ready. Loading chrome from").
   - Login URL: `grep -oE 'http://127\.0\.0\.1:[0-9]+/login\?one_time_code=[A-Za-z0-9_-]+' /tmp/minds-local.log | tail -1` -- if needed to drive via curl.

4. To stop: `pkill -9 -f "electron.*apps/minds"` (the minds forward subprocess dies with the parent).

**Caveats:**
- Running from local source ignores the installed /Applications/minds.app — the dev instance has its own user-data-dir under `~/Library/Application Support/minds` (same path as the packaged app, so the two collide if both run). Kill the packaged one first if it's running: `pkill -9 -f "/Applications/minds.app"`.
- If auth fails because the login code was consumed, restart the backend (step 1 + 2) to mint a fresh one.
- Draft-mode build (local dev) disables the auto-updater -- expect `@todesktop/runtime: skipping autoUpdater initialization because the build isn't released`. Not a bug.

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
   BASELINE_LOGIN_COUNT=$BASELINE bash apps/minds/scripts/drive-minds.sh > /tmp/drive-minds.out 2>&1 &
   disown
   ```
   The harness auths via the Login URL, POSTs to `/api/create-agent`, and polls `/api/create-agent/<id>/status` until DONE or FAILED. Override the template branch with `GIT_BRANCH=<branch>` (default: `pilot`) or the repo with `GIT_URL=<url>`.

3. Monitor `/tmp/drive-minds.out` for `status=DONE` / `FAILED`. Report the agent id + final state.

4. Default branch: `pilot`. If origin/main of forever-claude-template has a regression (e.g. the historical `--memory=4GiB` lima bug), point at a known-good branch via `GIT_BRANCH=...`.

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

## Sub-command: interact

Trigger: "interact", "drive UI", "automate", "click", "send message in chat".

Drive the minds desktop app's UI autonomously via Chrome DevTools Protocol (CDP). Useful for verifying chat UI behavior, reproducing button-click bugs, and capturing browser console logs without asking the user to manually click through.

**Setup (once per session):** launch Electron with `--remote-debugging-port=9222` instead of plain `pnpm start`. The existing `launch` sub-command does *not* enable CDP, so for `interact` you must relaunch:

```
pkill -9 -f "electron .*apps/minds" 2>/dev/null; sleep 2
(cd apps/minds && unset ELECTRON_RUN_AS_NODE && source ~/.zshrc 2>/dev/null && unset ELECTRON_RUN_AS_NODE && ./node_modules/.bin/electron . --remote-debugging-port=9222) > /tmp/minds-local.log 2>&1 &
disown
sleep 12
curl -s http://localhost:9222/json/list | python3 -c 'import json,sys; [print(x["type"], x.get("url",""), x["webSocketDebuggerUrl"]) for x in json.load(sys.stdin)]'
```

Pick the `page` target whose URL points at the chat UI (e.g. `.../system_interface/`) — that's the `webSocketDebuggerUrl` you'll connect to. It's also OK to connect to `/_chrome` to drive the sidebar/titlebar.

**Typical interaction pattern:** connect via `websockets`, subscribe to `Runtime.consoleAPICalled` to capture `console.log`, evaluate JS in the page context to type / click / read state. Skeleton:

```python
# /tmp/cdp_interact.py -- adapt per task
import asyncio, json, websockets

TARGET_WS = "ws://localhost:9222/devtools/page/<ID>"  # from /json/list

async def main():
    async with websockets.connect(TARGET_WS, max_size=10_000_000) as ws:
        logs, next_id, pending = [], [100], {}

        async def send(method, params=None):
            next_id[0] += 1; mid = next_id[0]
            fut = asyncio.get_event_loop().create_future(); pending[mid] = fut
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return await fut

        async def reader():
            while True:
                try: d = json.loads(await ws.recv())
                except websockets.ConnectionClosed: return
                if "id" in d and d["id"] in pending: pending.pop(d["id"]).set_result(d)
                elif d.get("method") == "Runtime.consoleAPICalled":
                    p = d["params"]
                    logs.append(f"[{p.get('type')}] " + " | ".join(str(a.get('value', a.get('description', ''))) for a in p.get('args', [])))

        asyncio.create_task(reader())
        await send("Runtime.enable"); await send("Page.enable")
        await send("Page.reload", {"ignoreCache": True})  # bypass cached bundle / SW
        await asyncio.sleep(5)

        # Example: type + click send. Uses InputEvent so Mithril's oninput fires
        # (.value=... alone won't -- Mithril relies on event.target.value in the
        # handler, and the onclick wiring is per-agent state, so whichever tab
        # rendered last governs what's typed).
        await send("Runtime.evaluate", {"expression": r'''
        (async () => {
            const tb = document.querySelector('.message-input-textbox');
            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
            setter.call(tb, 'hello from cdp');
            tb.dispatchEvent(new InputEvent('input', {bubbles:true, data:'hello from cdp', inputType:'insertText'}));
            await new Promise(r => setTimeout(r, 500));
            document.querySelector('.message-input-send-button')?.click();
            await new Promise(r => setTimeout(r, 1500));
        })()
        ''', "returnByValue": True, "awaitPromise": True})
        await asyncio.sleep(1)
        print("\n".join(logs[-40:]))

asyncio.run(main())
```

Run with `uv run --with websockets python3 /tmp/cdp_interact.py`.

**Recipes:**

- **"send a message in chat"**: as above. `InputEvent('input')` is mandatory for Mithril (synthetic `.value=...` alone won't propagate). The send button only renders when `messageText.trim().length > 0`, so always type first.
- **"click tab X" / "navigate to workspace Y"**: find the tab element by visible text with `Array.from(document.querySelectorAll('.dv-tab')).find(el => el.textContent.trim() === 'X')?.click()`.
- **"inspect what URLs the page is requesting"**: monkey-patch `XMLHttpRequest.prototype.open/send` + `window.fetch` before triggering the action. Store into `window.__xhrs` / `window.__fetches` and read after.
- **"check what event listeners are on element"**: use `DOMDebugger.getEventListeners` over a resolved objectId (enable `DOM.enable` + `Runtime.enable` first).
- **"capture console errors"**: `Runtime.consoleAPICalled` covers log/warn/error. `Runtime.exceptionThrown` covers uncaught errors; subscribe separately.
- **"inspect frontend bundle behavior live"**: the compiled JS is in `/home/weishi.guest/.local/share/uv/tools/minds-workspace-server/lib/python3.12/site-packages/imbue/minds_workspace_server/static/assets/index-*.js` inside the VM. Add `console.log` to the source in `~/Developer/imbue/forever-claude-template/vendor/mngr/apps/minds_workspace_server/frontend/src/`, `npm run build` in `frontend/`, then `limactl copy ...` the new JS + `index.html` into the installed static dir for hot-patching. Reload via `Page.reload({ignoreCache:true})` to serve the new bundle.

**Caveats:**

- Mithril uses `addEventListener('click', eventsObject, false)` where `eventsObject` is an `EventListenerObject` -- `element.onclick` stays `null`. Don't use `.onclick = ...` overrides.
- DockView creates one Mithril root per tab. If you see repeated `view()` calls with different `agentId` values, that's each tab's ChatPanel redrawing. Per-agent state must be keyed explicitly (see `messageTextByAgent` in `MessageInput.ts` for the pattern).
- The chat UI registers a service worker. Cached responses + cached bundle survive across launches. `Page.reload({ignoreCache: true})` + `Network.clearBrowserCache` clear the HTTP cache; for SW changes, `ServiceWorker.unregister` the old SW first.
- The login URL is one-time-use. Electron consumes it on its first chrome-view load. If you need to auth a second client (e.g. a separate curl), restart the Python backend (`minds forward`) to mint a fresh code.

**Do not**: use this for destructive actions (delete workspace, kill agent, push anywhere). CDP is an observation / light-interaction tool; for state-changing ops prefer direct API calls you can audit.

**When the prompt needs a workspace that doesn't exist yet**: don't drive the Create form via CDP -- the HTTP path is faster (~1:30 cold, ~30s warm) and more deterministic. Compose: run the `create agent` sub-command first, wait for `status=DONE`, then connect CDP to the resulting workspace's `/forwarding/<agent_id>/system_interface/` target. Same for cleanup: `delete agent <id>` via `mngr destroy` has real cleanup (VM + remote branch); the UI's X button only detaches.

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
