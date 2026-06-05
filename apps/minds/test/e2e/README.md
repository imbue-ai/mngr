# minds.app end-to-end tests (Playwright)

UI-driven E2E tests that launch a packaged `minds.app` Electron build and
drive its chat panel through Playwright. Replaces the brittle HTTP+CLI
`scripts/first-message-verify.sh` path with the actual user UI surface.

## Scenarios

| Spec | Scope | Requires lima? | Where it runs |
|---|---|---|---|
| `launch-smoke.spec.js` | Launch app, assert chrome window + Python backend + create form mount | no | every commit, vanilla macOS runner |
| `chat-roundtrip.spec.js` | Create LIMA workspace, type a prompt, assert reply | yes (nested virt) | self-hosted `minds-runner` MacBook |

Future:
- `gmail-tool.spec.js` — drive latchkey OAuth approval + Gmail tool call
- `auto-update.spec.js` — `--runtime-simulate-updates=update-available` and assert the install-and-restart dialog appears

## Running locally

Node 24.15.0 is pinned (`engines.node`) so use nvm or volta:

```
export PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH"
cd apps/minds
pnpm install
```

**Quit your running minds.app first** — Playwright launches a fresh
Electron and the singleton lock would otherwise collide.

Then:

```
# launch-smoke only (fast, no lima):
pnpm exec playwright test --config=test/e2e/playwright.config.js launch-smoke.spec.js

# chat round-trip (needs ANTHROPIC_API_KEY, takes 5-15 min):
ANTHROPIC_API_KEY=sk-ant-... pnpm exec playwright test \
  --config=test/e2e/playwright.config.js chat-roundtrip.spec.js

# all specs:
pnpm test:e2e
```

The tests target `/Applications/Minds.app/Contents/MacOS/Minds` by
default. Override via `MINDS_APP_PATH` to point at a downloaded
pre-release build:

```
MINDS_APP_PATH=/tmp/Minds-260530xxxxx.app/Contents/MacOS/Minds \
  pnpm exec playwright test --config=test/e2e/playwright.config.js launch-smoke.spec.js
```

## Isolation

Each Playwright run sets a unique `MINDS_ROOT_NAME=minds-pw-<runId>` so
`paths.js::getMindsRootName()` resolves all state (cookies, venv, mngr
host_dir) under `~/.minds-pw-<runId>/`. The user's live `~/.minds/` is
never touched. The isolated dir is left on disk for postmortem; clean
up manually with `rm -rf ~/.minds-pw-*`.

## CI integration (next)

- `minds-macos-launch.yml` — runs `launch-smoke.spec.js` on GitHub's
  `macos-latest` runner. Truly cold every commit. ~5 min.
- `minds-launch-to-msg.yml` (existing) — extends the verify job to
  also run `chat-roundtrip.spec.js` on the self-hosted MacBook.
  Replaces the imperative `first-message-verify.sh`.

## Authoring notes

The Playwright `@playwright/test` and `playwright` packages must
resolve to the same version (1.60.0 currently); pnpm pinning is
explicit in `package.json` to prevent the dual-version dispatch error
("Playwright Test did not expect test() to be called here").

The chat panel itself is served from the in-VM `system_interface` and
reaches the laptop via `mngr forward` -> SSH tunnel. The Playwright
locator chain is `mainWindow.frameLocator('#content-frame')` for the
chrome iframe.

Trace + screenshots are retained on failure under
`test-results/playwright-html/`. View with
`pnpm exec playwright show-report test-results/playwright-html`.
