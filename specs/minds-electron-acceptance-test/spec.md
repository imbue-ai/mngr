# minds-electron acceptance test for local docker workspace

## Overview

- Add a single acceptance test that drives the real Electron minds app end-to-end and confirms a local Docker workspace can be created from `forever-claude-template` (FCT).
- Replace the existing skipped `test_create_agent_e2e` in `apps/minds/test_desktop_client_e2e.py` and all of its helpers — that code never drove Electron, used a synthetic template, and its skip reason (TUI send-enter hang) is stale (FCT no longer sets `--message /welcome` on the services agent; window 0 is `sleep infinity && claude`).
- Drive Electron through Playwright via CDP: launch `apps/minds/electron/main.js` with `--remote-debugging-port`, connect with `playwright.chromium.connect_over_cdp(...)`, click through the Create form, wait for the workspace to come up.
- Use whatever minds env the runner has activated (`MINDS_ROOT_NAME` / `MINDS_CLIENT_CONFIG_PATH` inherited from the shell), defaulting to `minds-staging` if nothing is set. No per-test minds env isolation.
- Resolve FCT through a three-step fallback chain: `.external_worktrees/forever-claude-template/` → branch on the FCT public remote matching the current mngr branch → `main` on the FCT public remote.
- Marked `@pytest.mark.acceptance` (plus `docker`, `docker_sdk`, `tmux`); runs on every PR in CI under `xvfb-run`.

## Expected Behavior

- Running `uv run pytest apps/minds/test_desktop_client_e2e.py::test_create_local_docker_workspace_via_electron` (under `xvfb-run -a` on Linux) launches the dev-mode Electron app, drives the UI, and asserts the workspace's `system_interface` renders.
- The test inherits whatever `MINDS_ROOT_NAME` / `MINDS_CLIENT_CONFIG_PATH` are set in the shell. If `MINDS_ROOT_NAME` is unset, it defaults to `minds-staging` (and the matching `MINDS_CLIENT_CONFIG_PATH` for that tier). The test does **not** create or destroy a minds env; it shares the activated one.
- FCT resolution order, applied at test start:
  1. If `<repo-root>/.external_worktrees/forever-claude-template/` is a populated git working tree, that path is used directly. Nothing is fetched.
  2. Otherwise, the test runs `git ls-remote --heads https://github.com/imbue-ai/forever-claude-template.git <current-mngr-branch>` (from `git rev-parse --abbrev-ref HEAD` in `<repo-root>`). If the branch exists on FCT, the test shallow-clones it into `tmp_path / "fct"` and uses that.
  3. Otherwise, the test shallow-clones FCT `main` into `tmp_path / "fct"` and uses that.
- The resolved local filesystem path is passed as the `git_url` field of the Create form (the form accepts local paths; this matches `MINDS_WORKSPACE_GIT_URL` semantics).
- The Electron app is launched in dev mode (`paths.isDev()` is true because the test runs against the unpacked monorepo, not a packaged `.app`). Electron spawns the backend itself via `backend.js`, parses the `login_url` JSONL event from stdout, and the test's CDP-driven page lands on the home screen authenticated.
- The Create form is pre-filled via `MINDS_WORKSPACE_GIT_URL` + `MINDS_WORKSPACE_NAME` env vars threaded into the Electron child (works in dev tiers only; for `minds-staging` and other shared tiers the test types the values explicitly via Playwright because `_dev_only_workspace_default` ignores the env vars in those tiers).
- The agent name is `f"forever-{uuid4().hex[:8]}"` so parallel runs do not collide.
- After clicking Submit, the page navigates through `/creating/<agent_id>` to `/agents/<agent_id>/`; the test asserts a stable dockview DOM marker is visible (e.g. `[data-dockview]` or the empty-state element introduced by `apps/system_interface/.../DockviewWorkspace.ts`).
- On test exit (pass or fail), the test best-effort runs `mngr destroy <agent_name> --force` and shuts the Electron app down cleanly. A leftover Docker container from a crashed prior run is **not** auto-cleaned (documented limitation; the unique per-run agent name avoids active collisions).
- `@pytest.mark.timeout(900)` (15 min) caps the test. First-run cost (FCT Dockerfile build) fits inside this on the test runner; cached runs are much faster.
- The `xvfb` system package is a documented CI prerequisite. The test does not skip when `DISPLAY` is unset; it expects `xvfb-run -a` to provide one. A `just` recipe (`just minds-test-acceptance` or similar) wraps the invocation with `xvfb-run` for convenience.

## Implementation Plan

### Files to delete (in `apps/minds/test_desktop_client_e2e.py`)

Replace the entire file. The following symbols are removed:

- `_REPO_ROOT`, `_TEMPLATE_GIT_URL`, `_SIGNAL_FILE`, `_MINIMAL_TEMPLATE_SETTINGS` constants
- `minds_template_repo` fixture
- `_AGENT_NAME` constant
- `_get_template_repo`, `_configure_logging`, `_load_env`, `_find_free_port`, `_destroy_agent`, `_create_agent_with_retry`, `_wait_for_web_server` module-level helpers
- `DesktopClientFixture` class
- `test_create_agent_e2e` function and its `@pytest.mark.release`/`@pytest.mark.skip` stack
- The `/tmp/minds-e2e-done` signal-file pause mechanism

### Files to create / modify

#### `apps/minds/test_desktop_client_e2e.py` (rewritten from scratch)

Module docstring explains the new purpose: drive the Electron app via Playwright CDP to verify local Docker workspace creation against FCT.

Module-level constants:
- `_REPO_ROOT: Final[Path]` = `Path(__file__).resolve().parents[2]`
- `_FCT_EXTERNAL_WORKTREE: Final[Path]` = `_REPO_ROOT / ".external_worktrees" / "forever-claude-template"`
- `_FCT_REMOTE: Final[str]` = `"https://github.com/imbue-ai/forever-claude-template.git"`
- `_FCT_FALLBACK_BRANCH: Final[str]` = `"main"`
- `_DEFAULT_MINDS_ROOT_NAME: Final[str]` = `"minds-staging"`
- `_ELECTRON_LAUNCH_TIMEOUT_SECONDS: Final[int]` = `120`
- `_LOGIN_NAVIGATION_TIMEOUT_SECONDS: Final[int]` = `120`
- `_CREATE_FORM_TIMEOUT_SECONDS: Final[int]` = `600`
- `_SYSTEM_INTERFACE_TIMEOUT_SECONDS: Final[int]` = `180`

Module-level helpers (all sync, no fixtures):

- `_resolve_fct_path(tmp_path: Path) -> Path` — implements the 3-step chain. Returns an absolute `Path` to a populated git working tree.
  - Step 1: `if _FCT_EXTERNAL_WORKTREE.is_dir() and (_FCT_EXTERNAL_WORKTREE / ".git").exists(): return _FCT_EXTERNAL_WORKTREE`.
  - Step 2: read the current mngr branch via `subprocess.run(["git", "-C", str(_REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"], ...)`. Probe FCT via `subprocess.run(["git", "ls-remote", "--heads", _FCT_REMOTE, branch], ...)`; if stdout is non-empty, shallow-clone into `tmp_path / "fct"`.
  - Step 3: shallow-clone `_FCT_FALLBACK_BRANCH` into `tmp_path / "fct"`.
  - All git calls use `subprocess.run(..., check=True, capture_output=True, timeout=120, text=True)`.
- `_find_free_port() -> int` — bind socket, return port (one-shot helper used to allocate the CDP debug port).
- `_resolve_minds_env(monkeypatch: pytest.MonkeyPatch) -> None` — if `MINDS_ROOT_NAME` is unset, sets it to `_DEFAULT_MINDS_ROOT_NAME` and the matching `MINDS_CLIENT_CONFIG_PATH` (looked up via `imbue.minds.config.loader.repo_tier_client_config_path("minds-staging")` if such a helper exists, otherwise via a tier→path map kept in this module). Otherwise leaves them alone.
- `_launch_electron(workspace_git_url: Path, agent_name: str, debug_port: int) -> subprocess.Popen[bytes]` — `subprocess.Popen(["apps/minds/node_modules/.bin/electron", "apps/minds/electron/main.js", f"--remote-debugging-port={debug_port}"], cwd=_REPO_ROOT, env=…)`. The env dict layers: inherited `os.environ`, the resolved `MINDS_ROOT_NAME` / `MINDS_CLIENT_CONFIG_PATH`, `MINDS_WORKSPACE_GIT_URL=str(workspace_git_url)`, `MINDS_WORKSPACE_NAME=agent_name`, and unsets `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` (matching `just minds-start`). Pipes stdout/stderr through `logger.debug`.
- `_wait_for_cdp(debug_port: int, timeout_seconds: int) -> None` — polls `http://127.0.0.1:{debug_port}/json/version` until it responds; raises with a useful message on timeout.
- `_destroy_agent_quietly(agent_name: str) -> None` — best-effort `subprocess.run(["uv", "run", "mngr", "destroy", agent_name, "--force"], …)` that logs and swallows failures.

The single test:

- `test_create_local_docker_workspace_via_electron(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None`
  - Marks: `@pytest.mark.acceptance`, `@pytest.mark.docker`, `@pytest.mark.docker_sdk`, `@pytest.mark.tmux`, `@pytest.mark.timeout(900)`.
  - Configures loguru to stderr at DEBUG.
  - Calls `_resolve_minds_env(monkeypatch)`.
  - `fct_path = _resolve_fct_path(tmp_path)`.
  - `agent_name = f"forever-{uuid4().hex[:8]}"`.
  - `debug_port = _find_free_port()`.
  - `with sync_playwright() as p:` … `electron_proc = _launch_electron(fct_path, agent_name, debug_port)`. Pushed onto a `contextlib.ExitStack` (or a `try/finally`) so the process and the Playwright connection are torn down even on assertion failure.
  - `_wait_for_cdp(debug_port, _ELECTRON_LAUNCH_TIMEOUT_SECONDS)`.
  - `browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")`.
  - Find the first non-`devtools://` page in `browser.contexts[0].pages` (poll up to `_LOGIN_NAVIGATION_TIMEOUT_SECONDS` because Electron may still be spawning the backend and navigating).
  - Wait for the URL to match the `/` or `/create` route on the backend (the auth handshake completes via the `/login?one_time_code=...` redirect Electron loads on startup).
  - If we land on `/`, click the "Create workspace" affordance (or `goto("<backend>/create")` if explicit nav is easier).
  - On `/create`: assert the form's `git_url` field reads back `str(fct_path)` (env-var prefill in dev tiers) or type it explicitly via `page.fill('[name="git_url"]', str(fct_path))` (shared tiers). Same for `agent_name`. Leave `launch_mode=DOCKER` (default with no account) and `ai_provider=SUBSCRIPTION` (default with no account).
  - Click Submit. Wait for the URL to reach `/agents/<some-agent-id>/` within `_CREATE_FORM_TIMEOUT_SECONDS`.
  - Capture the agent id from the URL via regex.
  - Wait for a stable dockview DOM marker (e.g. `page.locator('[data-dockview]').first.wait_for(state="visible", timeout=_SYSTEM_INTERFACE_TIMEOUT_SECONDS * 1000)`).
  - `finally`: close the Playwright browser CDP connection, terminate the Electron process (SIGTERM then SIGKILL with a 5s grace), call `_destroy_agent_quietly(agent_name)`.

#### `justfile`

Add a recipe:

```
# Run the minds Electron acceptance test under xvfb (Linux).
minds-test-electron *args:
    xvfb-run -a uv run pytest apps/minds/test_desktop_client_e2e.py::test_create_local_docker_workspace_via_electron -v {{args}}
```

This is the documented way for operators to run the test locally; CI invokes the same wrapper.

#### `apps/minds/test_desktop_client_e2e.py` imports

- `os`, `re`, `socket`, `subprocess`, `sys` from stdlib
- `pathlib.Path`, `typing.Final`, `uuid.uuid4`, `contextlib.ExitStack`
- `pytest`, `loguru.logger`
- `playwright.sync_api.sync_playwright`, `playwright.sync_api.expect`
- `imbue.minds.config.loader` (only if a tier→client-config helper exists)

No imports from `imbue.minds.desktop_client.agent_creator`, `auth`, `app`, etc. — we drive the running Electron-spawned backend, not an in-process FastAPI app.

#### Docs

- Add a one-line entry to `apps/minds/docs/desktop-app.md` under a new "Testing" subsection pointing at `just minds-test-electron`.
- Update the existing `changelog/mngr-add-minds-electron-test.md` (already present) to reflect the final scope.

#### CI

- CI must install `xvfb` and the Playwright browsers (`uv run playwright install chromium`). Both already happen in some flows; the test's marks (`acceptance + docker + docker_sdk + tmux`) gate it to runners that already have docker, so adding `xvfb` is the only delta. Out of scope for this PR if CI image changes are tracked separately — if so, this becomes an Open Question to land alongside.

### Data types (none new)

No new pydantic models, primitives, or enums. The test is procedural Playwright orchestration; everything it needs already exists in the desktop client and FCT.

### What is *not* changed

- No changes to `apps/minds/imbue/minds/desktop_client/*.py` (the server, agent creator, auth, templates).
- No changes to `apps/minds/electron/*.js` (the Electron shell).
- No changes to `apps/minds/imbue/minds/testing.py` — the FCT-resolution helper is test-local.
- No changes to `forever-claude-template` itself.

## Implementation Phases

### Phase 1: Bring up an FCT worktree locally and prove the test scaffolding works

- Manually populate `<repo-root>/.external_worktrees/forever-claude-template/` via `git worktree add` from the operator's `~/project/forever-claude-template` clone, pointing at `main`.
- Stub `test_create_local_docker_workspace_via_electron` to: resolve FCT path, log it, launch Electron, connect via CDP, navigate to the home page, assert it loaded.
- Verify `xvfb-run -a uv run pytest …` succeeds end-to-end through "Electron home page loaded". This proves the Playwright CDP + Electron + activated-env wiring works in isolation, before adding the docker-workspace path.

### Phase 2: FCT resolution chain

- Implement `_resolve_fct_path` with the full 3-step chain.
- Manually unit-verify each branch by:
  - Branch 1: leave `.external_worktrees/forever-claude-template/` in place; confirm test uses it.
  - Branch 2: temporarily move the worktree aside, switch the mngr repo to a branch that exists on FCT, run; confirm shallow clone happens.
  - Branch 3: switch mngr to a branch that does NOT exist on FCT (e.g. the current `mngr/add-minds-electron-test` branch); confirm fall-through to `main`.

### Phase 3: Drive the Create form

- Extend the test to navigate to `/create`, fill in `git_url` + `agent_name` (handling both env-var-prefill and explicit-type paths), submit, and wait for the redirect to `/agents/<id>/`.
- This is where the heavy lift happens: the test must wait for `mngr create` to provision a Docker container, run the bootstrap, and bring the `system_interface` service up.

### Phase 4: Assert system_interface renders + add cleanup

- Add the dockview DOM marker assertion.
- Add the `finally` block: close Playwright, terminate Electron, `mngr destroy`.
- Verify a clean second run (state from the first run is fully torn down).

### Phase 5: Wire up CI

- Add the `just minds-test-electron` recipe.
- Confirm CI installs `xvfb` and `playwright install chromium` for the test job that runs acceptance tests.
- Submit a draft PR run and watch CI exercise the full path.

### Phase 6: Tear down the placeholder

- Replace the placeholder `specs/minds-electron-acceptance-test/spec.md` body with the final spec (this file).
- Update `changelog/mngr-add-minds-electron-test.md` if the scope shifted.

## Testing Strategy

- This *is* the test. No additional pytest tests are added for the test itself (per the repo convention that test utilities are exercised by the tests that use them).
- Manual verification before declaring complete:
  - Run the test against each of the three FCT-resolution branches at least once locally, with `xvfb-run`, against an activated dev env (`minds-<your-user>-dev` or similar).
  - Confirm the test passes against `minds-staging` (default fallback) — this exercises the "no env activated" code path.
  - Confirm `mngr destroy` runs cleanly in `finally` even when the test body raised.
  - Watch the test run in CI under the docker-acceptance job at least once.
- Failure modes to consciously verify (manually, not in code):
  - FCT remote unreachable (e.g. offline) — test should fail fast with a clear error, not hang.
  - Docker daemon not running — test should fail fast on the create-form submit with a clear error.
  - Electron binary missing (`pnpm install` skipped) — test should fail at launch with a clear error pointing at the install step.
- No new unit tests in `_test.py` files — the spec adds zero new pure functions worth unit-testing. The FCT-resolution helper is straightforward subprocess plumbing best validated by the test it serves.
- The ratchet tests (`apps/minds/imbue/minds/test_ratchets.py`) must continue to pass after the rewrite. Likely impact: the rewrite removes ~250 LOC and adds ~150 LOC; no expected change to ratchet counts.

## Open Questions

- **CI image — does the existing acceptance runner already have `xvfb` installed, or does CI config need to land in this PR?** If CI config is required, the PR scope grows by a small `.github/workflows/*.yml` edit (or equivalent for whatever CI we use). Need to confirm by reading the existing CI definitions for the `acceptance` job.
- **`minds-staging` default fallback — is staging actually reachable from CI?** If staging requires VPN / auth / a deploy that isn't running, the default falls flat for CI runs. The other interpretation of the user's answer ("use whatever is specified, fall back to staging") may have been intended for *operator* local runs; CI may need a different default (or an explicit activation step in the workflow). Confirm before relying on staging.
- **Should the test also be in `release` in addition to `acceptance`?** Release runs are a superset of acceptance, so marking only `acceptance` already means it runs in release too. But the deleted test was `release`-only; the deliberate downgrade is worth a sanity check.
- **What dockview DOM marker is most stable to assert on?** `[data-dockview]` is a guess based on the dockview library; the actual marker depends on what `apps/system_interface/.../DockviewWorkspace.ts` renders. Need a 30-second poke through the rendered HTML to lock the selector.
- **First-run docker image build cost.** The 15-minute timeout assumes the FCT Dockerfile build fits comfortably on CI. If the runner is slow or the image cache is cold, we may need to either bump the timeout, pre-bake the image in a CI setup step, or accept occasional first-run flakiness.
- **Leftover container cleanup across runs.** A crashed prior run leaves a `mngr-…` Docker container that nothing reaps; the next run's unique agent name avoids active collisions but the leak accumulates. Worth a follow-up PR to add a pre-run sweep (`mngr cleanup` or similar). Not in scope here.
- **Activation env for CI.** If we lean on "use what's activated" and CI never activates anything, the default-to-staging path is the only one ever taken in CI. That makes the env-var-prefill branch of the Create form code path dead in CI (since staging isn't a dev tier). Confirm whether CI should activate a dev tier explicitly, or whether the explicit-Playwright-typing path is the canonical CI flow.
- **Playwright Electron driver vs CDP.** Python Playwright doesn't expose `_electron.launch` cleanly, so this spec uses CDP. If a future Playwright bump adds first-class Python Electron support, we may want to migrate; the helper functions are scoped tightly enough to make that a one-PR follow-up.
