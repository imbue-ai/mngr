# minds.app end-to-end tests (Playwright)

UI-driven E2E tests that launch a packaged `minds.app` Electron build and drive its chat panel through Playwright over CDP. Replaces the brittle HTTP+CLI `scripts/first-message-verify.sh` path with the actual user UI surface.

## What runs in CI

| Driver | What it asserts | Needs lima? | Runs in |
|---|---|---|---|
| `macos-launch.spec.js` (Playwright) | App launches; chrome window renders; Python backend binds; the home "Create" link or the welcome splash ("Log In" link / "Continue without an account" button) is visible. Catches the `_MNGR_FORWARD_LISTEN_TIMEOUT_SECONDS` class of regression that Tart used to catch by hand. | no | `minds-launch-to-msg.yml` `macos_launch` job on `macos-latest`, twice-daily schedule + dispatch. ~5 min. |
| `scripts/launch_to_msg_e2e.py` (Python over CDP) | Drives the full Electron flow: launch → auth → create LIMA workspace → first agent message → assert nonce-verified reply. Also runs the slack-permission-flow sub-scenario (mock Slack via /etc/hosts + cert + socat, see below). | yes (nested virt) | `minds-launch-to-msg.yml` `verify` job on the self-hosted `minds-runner` MacBook. |

## Running locally

Node 24.15.0 is pinned (`engines.node`); use nvm or volta:

```
export PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH"
cd apps/minds
pnpm install
```

**Quit your running minds.app first** — Playwright launches a fresh Electron and the singleton lock would otherwise collide.

```
# fast smoke (no lima):
pnpm exec playwright test --config=test/e2e/playwright.config.js macos-launch.spec.js

# chat round-trip (needs ANTHROPIC_API_KEY, 5-15 min):
ANTHROPIC_API_KEY=sk-ant-... pnpm exec playwright test \
  --config=test/e2e/playwright.config.js chat-roundtrip.spec.js

# all specs:
pnpm test:e2e
```

Default target is `/Applications/Minds.app/Contents/MacOS/Minds`. Override via `MINDS_APP_PATH` to point at a downloaded pre-release build:

```
MINDS_APP_PATH=/tmp/Minds-260530xxxxx.app/Contents/MacOS/Minds \
  pnpm exec playwright test --config=test/e2e/playwright.config.js macos-launch.spec.js
```

## Isolation

Each Playwright run sets a unique `MINDS_ROOT_NAME=minds-pw-<runId>` so `paths.js::getMindsRootName()` resolves all state (cookies, venv, mngr host_dir) under `~/.minds-pw-<runId>/`. The user's live `~/.minds/` is never touched. The isolated dir is left on disk for postmortem; clean up manually with `rm -rf ~/.minds-pw-*`.

## Authoring notes

- `@playwright/test` and `playwright` must resolve to the same version (1.60.0 currently). pnpm pinning is explicit in `package.json` to prevent the dual-version dispatch error ("Playwright Test did not expect test() to be called here").
- The chat panel itself is served from the in-VM `system_interface` and reaches the laptop via `mngr forward` → SSH tunnel. Playwright locator chain: `mainWindow.frameLocator('#content-frame')` for the chrome iframe.
- minds.app uses `BaseWindow + WebContentsView` (not a single `BrowserWindow`); `app.firstWindow()` returns the chrome view at URL `/_chrome`, which renders only the title bar. For full-content screenshots use `pickContentWindow` from `fixtures.js` — it polls `app.windows()` for a page on the backend origin without the `_chrome` prefix.
- Traces + screenshots retained on failure under `test-results/playwright-html/`. View with `pnpm exec playwright show-report test-results/playwright-html`.

## Slack mock (self-hosted job only)

The slack-permission-flow test intercepts `https://slack.com/api/*` calls at the host so the agent never reaches real Slack. Implementation lives entirely in the verify job (see `minds-launch-to-msg.yml`'s setup steps); design notes:

- /etc/hosts maps `slack.com` and `files.slack.com` to `127.0.0.1`.
- A self-signed cert for those hosts is installed in `/Library/Keychains/System.keychain` so the host's `libcurl-darwinssl` trusts it. Brew curl + `CURL_CA_BUNDLE` is used in place of the system curl that ignores `--cacert`.
- `slack-mock-server.js` listens on `127.0.0.1:8443` (plain HTTP); a sudo'd `socat OPENSSL-LISTEN:443,reuseaddr,fork TCP:localhost:8443` terminates TLS and forwards.
- `latchkey auth set slack -H "Authorization: Bearer mock-tok"` pre-seeds creds against the bundled latchkey shim with `LATCHKEY_DIRECTORY=$HOME/.minds/latchkey`.
- Teardown reverses /etc/hosts, removes the trusted cert, clears the latchkey slack auth, and kills mock + socat. Runs in an `always()` step so a failed test still cleans the runner.

The agent's latchkey gateway runs on the macOS host (started by minds.app), not inside the lima VM — the agent reaches it via reverse-SSH back to `127.0.0.1:1989` and the host's gateway makes the outbound `slack.com` call. All interception lives on the host for that reason.

Earlier-considered alternatives that didn't work: patching `slack.js` in the signed bundle (Gatekeeper rejects); registering a `slack-mock` latchkey service (agent doesn't auto-discover; would also miss the user's actual slack tool-call path).
