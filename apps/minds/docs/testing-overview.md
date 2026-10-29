# Imbue Studio testing overview

This is a map of every kind of test the Imbue Studio app has, where each kind runs, and
a backlog of end-to-end (e2e) tests worth adding -- with emphasis on tests that
fit the modal-snapshot CI stage (a pre-baked workspace-in-Docker image that lets
e2e tests fan out in parallel through offload).

The test taxonomy and locations follow the repo `style_guide.md` ("Types of
tests"): unit (`*_test.py`), integration (`test_*.py`, unmarked), acceptance
(`@pytest.mark.acceptance`), release (`@pytest.mark.release`), and smoke
(`@pytest.mark.smoke`, see 1.10). Imbue Studio adds a few app-specific markers
(below).

Tests that verify a behavior unit declare it with the
`witnesses(coordinate, partial=...)` marker: the coordinate names a unit in the
behavior corpus under `apps/minds/behaviors/`, and `partial=` notes what the test does
not cover. The marker is registered in the shared pytest settings, so any
project in the monorepo can use it; the behaviors skill
(`.agents/skills/behaviors/SKILL.md`) defines the convention,
`behaviors.md` (in this folder) covers the CLI, and
`uv run mngr behaviors matrix --root apps/minds/behaviors` reports per-unit coverage
from the markers.

## Part 1 -- Where the tests live and where they run

### 1.1 Python unit / integration tests (`*_test.py`)

~80 files under `apps/minds/`, all unmarked, collected by the default offload
run. By area:

- **CLI** (`imbue/minds/cli/`): `_activated_env_test.py`, `env_test.py`,
  `paid_test.py`, `pool_test.py`, `run_test.py`.
- **Config** (`imbue/minds/config/`): `data_types_test.py`, `loader_test.py`.
- **Desktop client** (`imbue/minds/desktop_client/`): ~45 files -- auth, server,
  the `/api/v1` surface (`api_v1_test.py`), backup/restic, latchkey
  (`latchkey/.../*_test.py`), recovery, templates, workspace ops, SSH grants
  (`workspace_ssh_test.py`), etc. Note `test_desktop_client.py` is, despite its
  `test_` prefix, a large **unmarked** Flask route suite (~140 functions).
- **Envs** (`imbue/minds/envs/`): `docker_cleanup_test.py` (the only `envs` test
  carrying `@pytest.mark.docker`/`docker_sdk`), plus `generation_test.py`,
  `health_check_test.py`, `provisioning_test.py`, `recover_test.py`,
  `secret_lifecycle_test.py`, `vault_reader_test.py`, and providers tests.
- **Utils / misc**: `bootstrap_test.py`, `build_info_test.py`, `main_test.py`,
  `utils/*_test.py`, `scripts/build_test.py`.
- **Ratchets**: `imbue/minds/test_ratchets.py` (~65 `test_prevent_*` checks,
  `xdist_group(name="ratchets")`).

### 1.2 Marked Python suites (`test_*.py`)

| File / test | Marks | What it exercises |
|---|---|---|
| `test_aws_workspace_release.py::test_aws_workspace_runs_in_runsc_container_on_ec2` | `release`, `timeout(900)`, skip unless AWS creds + `MNGR_AWS_RELEASE_TESTS=1` | Provisions a real EC2 instance, asserts the agent runs in a runsc/gVisor container. Costs money. |
| `test_snapshot_resume.py` (16 tests) | each `minds_snapshot_resume` + `docker` (+ `rsync` on the electron test) + per-test `timeout` | Most assert against a Modal-snapshot sandbox (pre-baked, stopped DEFAULT_WORKSPACE_TEMPLATE workspace container): resume sanity checks, the backup-update chat gate against a live LLM-backed chat, the backup-service check/update/force-update converge loop (real supervisord + `official`-remote tag fetch from GitHub), the backup enable / env-repair / destination-change flow (real minds-side restic provisioning + `mngr exec` injection; installs a pinned restic on the sandbox host when the image lacks the bundled one), and the in-place backup restore (the real restore worker + workspace script: pinned-restic install, safety snapshot, sync restore, services back). `test_create_workspace_and_sign_in_via_modal_then_chat_via_electron` reuses the snapshot image's warm Electron/Playwright/Xvfb toolchain to drive the real Electron app: it creates a fresh local Docker DEFAULT_WORKSPACE_TEMPLATE workspace (which boots with no AI credentials, open on the welcome chat the creation page seeded with the onboarding conversation), sends that chat a message, signs in through the provider chooser that first send opens in the chat's own frame (the Anthropic API-key path) with a raw API key (needs `ANTHROPIC_API_KEY`), and asserts the agent answers the message. It runs under a minds root of its own, so its containers never share the snapshot workspace's name prefix, and removes them in `finally`. Shares its driver with `desktop_client/e2e_workspace_runner.py`. Only via `just test-offload-minds-snapshot` (or `just minds-test-electron` locally). See 1.5. |
| `test_workspace_programs_e2e.py` | `minds_snapshot_resume` + `docker` + `timeout(600)` | Runs workspace programs inside the snapshot's real DEFAULT_WORKSPACE_TEMPLATE workspace container, through the same zip-application command the app ships them with: the backup gate probe answers without a `gate_error`, and the update program's refreshing `read` installs its own archive as `dispatcher.pyz` and the cron line, whose `tick` then runs from that archive. Only via `just test-offload-minds-snapshot`. |
| `test_sse_redirect.py::test_sse_redirect_on_done` | `release` | Werkzeug server + Playwright; verifies the creating-page SSE stream delivers `done` and the JS redirects. No Docker/agent. |
| `imbue/minds/test_claude_version_alignment.py::test_claude_code_version_matches_default_workspace_template_pin` | `release` | Checks the Claude Code CLI pin matches the DEFAULT_WORKSPACE_TEMPLATE pin. |

### 1.3 Deployment-test suites (`deployment_tests/`)

An importable helper package, excluded from all offload runs and `test-quick`;
driven only by `just minds-test-deployment` and siblings (orchestrator
`apps/minds_admin/scripts/test_deployments.py`). Every test here carries
`@pytest.mark.release` (so it is part of the shared release suite, discoverable
by tag) in addition to its capability mark; all Imbue Studio release tests run from
the Imbue Studio jobs (`test-minds-release`), never from the mngr release workflow, which
excludes the whole `apps/minds` tree by path.

- `@pytest.mark.minds_deployment` (each mints its own ephemeral CI env):
  `test_deploy_new_version`, `test_deploy_auto_rollback_on_broken_healthcheck`,
  `test_deploy_then_destroy_round_trip`.
- `@pytest.mark.minds_services` (run against a pre-stood-up shared env):
  `test_logged_in_routes`, `test_litellm_spend_tracking_via_local_workspace`
  (currently `skip`), plus the remote-workspace pool tests
  (`test_pool_lease`, `test_pool_fast_path_create`,
  `test_workspace_stop_start`), which lease real pre-baked bare-metal
  slices -- the release dispatch pre-bakes them onto the standing CI boxes
  (see `specs/remote-workspaces-in-ci.md`).

### 1.4 JS / Electron tests (`apps/minds/test/`, `apps/minds/frontend/src/`)

Both suites below run from `just test-minds-js`, wired into CI as the
**`test-minds-js`** job (see 1.5). Neither is reachable from pytest, so that job
is the only thing that runs them.

- **Node unit** (`test/unit/*.test.js`): `node --test` suites for the pure
  Electron-shell helpers (startup routing, the loading document's intro
  schedule, surface routing, deeplinks, session persistence, log handling,
  platform roots, the data-directory migration, the embed contract, release
  channels) and for the `paths.js` export surface the Electron-only modules
  consume. Run alone via `pnpm test:unit`.
- **Frontend unit** (`frontend/src/**/*.test.ts`): vitest suites for the SPA's
  models and views, rendered without a DOM through the `renderRoot` helper in
  `frontend/src/testing.ts`. Run alone via `pnpm -C frontend test`.
- **Playwright e2e** (`test/e2e/`, `playwright.config.js`, `pnpm test:e2e`):
  - `macos-launch.spec.js` -- launches the installed `/Applications/Imbue Studio.app`
    via the `mindsApp` fixture. **The only JS spec** (wired into CI in
    `minds-launch-to-msg.yml`). The legacy renderer-contract specs were
    deleted with the pre-SPA shell scripts they drove.

### 1.5 CI map

`.github/workflows/ci.yml` (push to main + all PRs):

- **`check-changelog`** -- changelog gate.
- **`test-minds-js`** -- `just test-minds-js`, on PRs only and only when
  `apps/minds/electron`, `apps/minds/test/unit`, `apps/minds/frontend`,
  `apps/minds/package.json`, `apps/minds/pnpm-lock.yaml` or
  `apps/minds/todesktop.js` changed. The lockfile counts because
  `electron-updater` is pinned exactly, so moving it need not touch
  `package.json`. Node toolchain, so it runs on the orchestrator rather than in
  an offload sandbox.
- **`test-offload`** ("Unit + Integration Tests") -- `just test-offload`. Filter:
  `not acceptance and not release and not flaky and not sdk_live and not
  minds_deployment and not minds_services and not minds_snapshot_resume and not
  smoke`, plus a retrying `flaky` group.
- **`test-offload-acceptance`** ("Acceptance Tests") -- `acceptance and not
  docker and not docker_sdk and not minds_deployment and not minds_services and
  not minds_snapshot_resume and not smoke`; pre-creates a shared Modal env.
- **`test-docker`** -- real Docker daemon on a GitHub runner; `(docker or
  docker_sdk) and not release and not minds_snapshot_resume`.
- **`test-minds-linux-install`** -- the public-repo developer bootstrap: runs
  `apps/minds/scripts/install-linux.sh --yes --no-launch --skip-docker` from
  the checkout on a stock `ubuntu-latest` runner, then asserts the pinned
  Node and pnpm, the Electron dependencies, the bundled git at its pinned
  version, the built UI bundle, the `~/.local/bin/minds-desktop` launcher,
  and `uv run minds run --help`. Everything it touches is public, so the
  same job runs in the public-mirror overlay workflow.
- **`build-minds-snapshot` + `test-minds-snapshot`** ("Minds Snapshot Resume
  Tests") -- the modal-snapshot stage (see below). All `minds_snapshot_resume`
  tests run here, including the Electron create+chat test (which reuses the
  snapshot image's baked Electron toolchain) -- there is no longer a separate
  `test-docker-electron` job.
- **`cleanup-modal-environments`** -- sweeps old Modal test envs + leaked
  snapshot images.
- **`test-minds-release`** (manual only -- `workflow_dispatch` +
  `run_minds_release_tests`) -- the home for **all** Imbue Studio release tests. Runs
  the `minds_deployment` group via the deployment orchestrator (each mints +
  destroys its own ephemeral ci env), then the plain Imbue Studio `@release` tests that
  need no ci env, selected by tag: `-m 'release and not minds_deployment and not
  minds_services and not minds_snapshot_resume and not smoke'`. That is where
  `test_claude_version_alignment.py`, `test_sse_redirect.py` (Chromium installed
  in-job), and `test_aws_workspace_release.py` (skips without AWS opt-in) run.

`.github/workflows/release-tests.yml` (`workflow_dispatch` + `v*` tags) -- the
*mngr* release suite only. Both jobs exclude the whole `apps/minds` tree by path
(`--ignore apps/minds`); all Imbue Studio release tests run from `test-minds-release`
above (the Imbue Studio release procedure is a manual dispatch, not a `v*` tag):

- **`test-mngr-release-docker`** -- `(docker or docker_sdk) and release`, with
  `--ignore apps/minds`.
- **`test-mngr-release`** -- the `release` suite with `--ignore apps/minds`,
  matrixed `[ubuntu, macos] x group 1..12` (pytest-split).

`.github/workflows/minds-smoke.yml`: the smoke workflow -- validates the
published artifact against the deployed production environment after a deploy of
the tier services to production. It gates nothing; a failure alerts Slack. See
1.10 for its target, trigger, jobs, and credentials.

`.github/workflows/minds-launch-to-msg.yml`: builds the `.app` via ToDesktop,
runs `scripts/launch_to_msg_e2e.py` (Python launch-to-first-message + Slack), and
a parallel job runs `macos-launch.spec.js`. Both inputs (`commit_sha` for mngr,
`template_ref` for default-workspace-template) accept a full 40-char SHA, branch,
or tag; a ref is frozen to its SHA once at run start, and that frozen SHA is
what gets built (mngr) and what the agent is created from (DEFAULT_WORKSPACE_TEMPLATE) -- the SHAs in
the slack message and step summaries are exactly what ran, even if the ref
moved mid-run.

### 1.6 The modal-snapshot stage (the "new" parallel-in-offload e2e stage)

This is the `build-minds-snapshot` -> `test-minds-snapshot` job pair. Its whole
point is that **expensive workspace-in-Docker creation happens once per run**,
then cheap test sandboxes fan out from the baked image.

- **Build** -- `scripts/snapshot_minds_e2e_state.py` builds a Modal image with a
  warm Electron/Playwright/Xvfb + Docker-in-Docker toolchain, creates a sandbox with
  `experimental_options={"vm_runtime": True}` (true-VM runtime, so
  `/var/lib/docker` survives `snapshot_filesystem()`), starts `dockerd`, calls
  `e2e_workspace_runner.create_workspace_via_electron` directly (no pytest, **no**
  `mngr destroy`), then `docker stop`s for a deterministic stopped state, and
  snapshots the filesystem. The image id is recorded in a Modal-Dict cleanup
  ledger.
- **Test** -- `just test-offload-minds-snapshot "<image_id>"` ->
  `offload -c offload-modal-minds-snapshot.toml run --override-image-id <id>`.
  The config boots straight from the override image (no Dockerfile/post-patch),
  `cpu_cores=4.0`, `memory_gb=8`, `vm_runtime=true` (must match the producer),
  one `[groups.all]` with `filters="-m 'minds_snapshot_resume' -c pyproject.toml"`,
  and `[framework].paths` scoped to the files that carry the mark so local
  discovery takes seconds instead of a ~90s full-monorepo collection.
  `max_parallel=20`. The image is deleted on success. Both jobs are gated by the
  `DISABLE_MINDS_SNAPSHOT_CI` repo variable and skipped on fork PRs.
- **Currently runs:** every `minds_snapshot_resume` test in
  `test_snapshot_resume.py` (resume sanity checks, the Electron create+chat
  round-trip, the backup-update chat gate, the backup-service
  check/update and enable/repair/destination-change flows, and the in-place
  backup restore) and `test_workspace_programs_e2e.py` (a workspace program
  run in the real workspace). Run a single one locally with
  `just test-offload-minds-snapshot <image-id> '--filter <test_name>'`, or in
  CI via the `minds_snapshot_test_filter` workflow_dispatch input on ci.yml;
  mint an image id manually via `uv run python
  scripts/snapshot_minds_e2e_state.py`.

The `ANTHROPIC_API_KEY` is pulled from Vault for the test job (so the agent can
actually run), but these tests do **not** require an imbue_cloud login.

### 1.7 Fixtures available for new tests

- **Modal snapshot** (`apps/minds/conftest.py`): the session-scoped
  `running_workspace` fixture (returns an `imbue.minds.testing.RunningWorkspaceContainer`
  after starting the snapshot's stopped containers + waiting for
  `system_interface`; the workspace is chosen from the container states the
  sandbox recorded at resume, `snapshot_container_states`, never from whatever
  the daemon lists later). Run a workspace program inside it with
  `run_workspace_program_in_container` (`imbue/minds/testing.py`), and a shell
  command with `exec_in_workspace_container`. `test_snapshot_resume.py` adds the
  module-autouse `_ensure_dockerd_after_snapshot_resume`. Use marks
  `minds_snapshot_resume` + `docker`, and add a new file that carries the mark to
  `[framework].paths` in `offload-modal-minds-snapshot.toml`.
- **Live Electron workspace** (driver, not a fixture):
  `imbue/minds/desktop_client/e2e_workspace_runner.py` --
  `create_workspace_via_electron`, `destroy_agent_best_effort`,
  `resolve_default_workspace_template_path`, `materialize_isolated_default_workspace_template`, `ensure_minds_env_defaults`,
  `find_free_port`.
- **Deployment/services** (`deployment_tests/conftest.py`): `shared_env(role)`,
  `verified_user`, `ephemeral_env`, `signup_email` (mail.tm).
- **General Imbue Studio helpers** (`imbue/minds/testing.py`): `make_git_repo`,
  `init_and_commit_git_repo`,
  `stub_mngr_host_dir`, `extract_response`; `desktop_client/testing.py`
  (`restic_backup_a_file`); `utils/testing.py` (`RecordingMngrCaller`);
  `latchkey/testing.py` (`FakeLatchkeyGatewayClient`, `build_fake_gateway_client`).

### 1.8 Sleep/wake handling (manual, real laptop)

What the app does in the minutes after a laptop wakes cannot be checked in CI.
`scripts/sleep_wake_drill.py` stages the incident against running apps on a
spare Mac and reports what each did; see `sleep-wake-drill.md` (in this
folder).

### 1.9 Multi-device tests by hand (extra env roots on one machine)

A "second device" for a sync or sharing check is a second env root on the
same machine, made with `minds-admin env activate --create dev-<you>-b` and a
copy of the first root's `client.toml`, driven by its own `minds run`. Two
rules keep that from corrupting the workspaces under test:

- **One instance per env root, and never two roots signed in to the same
  account against the same workspaces at once.** Each root's `mngr latchkey
  forward` supervisor provisions the same remote machines, and the last pass
  overwrites the desktop-owned latchkey secrets on them while only one
  supervisor holds the tunnel they are checked against, so the agents there
  lose their permission channel with "Unauthorized". Run the second device
  only for the step that needs it.
- **Stop a root completely when its step is done.** `just minds-stop` stops
  the backend and, with it, the supervisor it owns; a supervisor an older
  build or a crash left holding the root is stopped by
  `uv run minds-admin env stop-local dev-<you>-b` (backend plus supervisor);
  `--list-only` reports what still holds a root without stopping it.

### 1.10 Smoke tests (`apps/minds/smoke_tests/`)

Smoke tests run *after* a deploy or a release, against the published artifact
and the deployed production environment, and never gate the release they
validate -- the repo `style_guide.md` ("Types of tests" -> "Smoke Tests") holds
that definition. This section is the operational half: what they target, what
triggers them, how their CI jobs are segmented, which credentials they hold, and
how to run them. The decisions behind the first shape are recorded in
`specs/minds-smoke-tests/spec.md`.

**The first shape.** One journey, driven through the installed Imbue Studio app
against the production backend: launch the published build and reach the landing
page; sign in to Imbue Cloud through the app's web-login flow, completing the
hosted login page in the test's own browser; create an Imbue Cloud workspace;
send the chat a prompt that builds a hello-world scaffolding app (a web server
with a route that answers 200, registered as a workspace app; the prompt is
optimized to be minimally expensive while still building that server); share the
workspace from within the app, open the app's route through the share, and assert
the 200 (the visitor is the owner -- grants are a later scenario); turn sharing
off from within the app as the original user; destroy the workspace.

Beside the journey, two cheaper tests:

- **Signup** -- completes the half of signup that production allows automation
  to drive: a throwaway account minted by the `setup` job verifies its email
  through mail.tm, and the download page then lists installers. Production's
  signup form has Turnstile and the waitlist on, so automation does not drive
  the form itself; the two accounts (one persistent, one throwaway) are the
  accepted shortcut.
- **Read-only probes** of the deployed surfaces: connector and LLM-proxy
  liveness and version, the hosted accounts pages, the web chrome and its
  `/open` page, the redirects off the legacy chrome hosts, the download
  redirect, and the release feed's channel manifests and installers.

**Target: `binary` and `backend` are fixtures.** Which artifact to test, and
which backend to test it against, are both fixtures, so a scenario is decoupled
from their selection and creation: `test_foo_1234(binary, backend)`. The
`binary` fixture resolves a published build from a release channel's feed
manifest, a local path, or the dev checkout's Electron; the `backend` fixture
resolves a tier's committed `client.toml` and `deploy.toml`, or an activated dev
env. In CI the target is **(alpha, production)** -- that pairing is the value the
suite adds. The channel and the tier are workflow inputs, so the target can vary
with the trigger later. Linux runs first, on `ubuntu-latest`, against the `.deb`
(an implementation detail, not a commitment); macOS is a fast follow.

**Trigger.** One: a deploy of the tier services to production, dispatched by a
successful `minds-admin env deploy` against production (`--skip-smoke` opts
out). A release-channel publish (`minds-release-channels.yml`) does not trigger
a run, so publishing a new app binary never starts one on its own. In CI the
manifest, the pytest log and the screenshots are uploaded as the run's artifact,
which `teardown` downloads. A daily run, when it is added, may carry a different
target (for example stable/production daily, alpha/production post deploy).

**Jobs.** Cleanup is the CI job's responsibility, segmented so the test job can
never delete resources and the cleanup happens regardless of what the test did:

- **`setup`** mints the throwaway account (and its mail.tm inbox) and hands it to
  the test job through a per-run Vault path.
- **`test`** holds only user credentials -- the persistent account and the
  throwaway account. It acts as a user, records every resource it creates in a
  run manifest, and never holds administrative or database credentials.
- **`teardown`** runs regardless: it releases any workspaces the run leased,
  turns off any shares left on, deletes the throwaway account (through
  the connector's own `DELETE /account`, as its holder), deletes the mail.tm inbox and the per-run Vault
  path, and sweeps stale smoke resources from earlier runs by their
  `studio-smoke-` prefix.
- **`notify_slack`** runs regardless and posts the outcome. Its webhook is read
  from Vault; the channel is configured later, and the workflow keeps working
  when the webhook is not set.

**Accounts.** The persistent account signs in, creates, builds, shares, and
destroys; it is on the ally plan, the only plan with an LLM budget. A script and
a `just` recipe create it on a tier and put its credentials in the right Vault
path -- the operator runs that once per tier, as part of deploying this suite.
The throwaway account exists for the signup test and is deleted by `teardown`.

**Mutual exclusion and the single rerun.** Runs are rival and exclusive: no two
smoke invocations run at the same time (a GitHub `concurrency` group per tier,
queued rather than cancelled). The test step gets one rerun before the run counts
as failed; `teardown` cleans up after both attempts.

**CI keys that talk to production.** This is the first CI workflow holding
production credentials. These are CI's keys, which happen to give one specific CI
job access to production; each one belongs to a named job of `minds-smoke.yml`:

| GitHub Environment | Vault role and policy | Reads | Writes |
|---|---|---|---|
| `minds-smoke-setup` | `ci_minds_smoke_setup_production_access` | `minds/production/supertokens/MINDS_ADMIN_KEY`, `minds/production/smoke/*` | `minds/ci/smoke-runs/*` |
| `minds-smoke-test` | `ci_minds_smoke_test_production_access` | `minds/production/smoke/*`, `minds/ci/smoke-runs/*` | nothing |
| `minds-smoke-teardown` | `ci_minds_smoke_teardown_production_access` | `minds/production/supertokens/{MINDS_ADMIN_KEY,SUPERTOKENS_CONNECTION_URI,SUPERTOKENS_API_KEY}`, `minds/production/neon/DATABASE_URL`, `minds/production/smoke/*`, `minds/ci/smoke-runs/*` | deletes `minds/ci/smoke-runs/*` |

Paths are under the `secrets` mount. The persistent account lives at
`minds/<tier>/smoke/{SMOKE_ACCOUNT_EMAIL,SMOKE_ACCOUNT_PASSWORD}`, the per-run
handoff at `minds/ci/smoke-runs/<run_id>/`, and the Slack webhook at
`mngr/ci/SLACK_MINDS_SMOKE_WEBHOOK` (read through the existing `mngr_ci_gh`
role). The roles and policies are defined in the imbue-ai/vault terraform repo.

**How the journey drives the app.** The build under test is whatever the
channel serves, so it cannot carry an accommodation for being driven from a
test: the suite makes the app's process look like an installed one instead.
It is launched from an empty directory outside any checkout, on an
environment composed by the launcher rather than inherited -- every
`PYTEST_*` variable (pytest's own marker, which mngr's config guard reads,
and the resource guards' per-test switches, which make a guarded binary exit
127), every `MNGR_*` / `MINDS_*` variable of the test process, the `UV_*`
switches the mngr test harness sets (`UV_OFFLINE` would forbid the app's
first-launch `uv sync` the managed CPython it downloads), the test venv, and
the resource guards' shim directory on `PATH` are dropped, and
`MINDS_DATA_HOME`, `MINDS_ROOT_NAME`, the client config and `DISPLAY` are set
explicitly. The `BROWSER` it is given is a no-op, so its web-login never
reaches the operator's own browser (which would hand whatever account is
signed in there to the app); the suite opens the hosted sign-in URL from the
start flow's "I have an account (log in)" aside and completes it in its own
headless Chromium.

The workspace is created in the region the form already points at, which is
the app's own choice for where this machine's workspaces run; a run therefore
fails when that region is out of pool capacity, which is a true thing to
report about the tier. The form shares a workspace on an account as it is
created unless its sharing switch is turned off; the journey turns it off, so
that its share step publishes the workspace from the Share tab.

A fresh workspace boots with no AI credentials. A user gives it the account's
own through the chat's provider chooser: its "Sign in with Imbue" lane floats
the desktop's AI-keys dialog, whose button mints a LiteLLM key and shows a
two-line credential blob to paste back into the lane. The suite calls the two
routes behind that lane directly -- `POST /settings/ai-keys/mint` on the
desktop, then `POST /api/claude-auth/submit-credentials` into the chat,
requiring the workspace to report back the proxied `imbue` auth mode -- without
driving the lane's own controls; exercising those is a follow-up recorded in
the spec. The prompt is then typed into the desktop's launcher field and sent
with Enter, which sends a text to the workspace's main chat (the chat app's
`main` launch path): in a fresh workspace that is the welcome chat, named
"Main", still awaiting its first send. The prompt is that first send, so it
launches the chat's agent on the account signed in by then. The suite reads
the main chat's id from the chat app's own `GET /api/chats` (`main_chat_id`)
before pressing Enter, then waits for that chat's page to show the prompt and
the reply.

**Running it locally.** The suite runs from a developer laptop against staging
or production with nothing but this branch, so a developer can see whether the
tier passes or fails before any CI exists. What a run needs:

- Vault: `vault login -method=oidc role=minds_staging` for staging; production
  needs the production role. The suite reads the persistent account from
  `secrets/minds/<tier>/smoke/{SMOKE_ACCOUNT_EMAIL,SMOKE_ACCOUNT_PASSWORD}` (or
  from `MINDS_SMOKE_ACCOUNT_EMAIL` / `MINDS_SMOKE_ACCOUNT_PASSWORD`);
  `minds-smoke-create-persistent-account` and `minds-smoke-setup` read
  `secrets/minds/<tier>/supertokens/MINDS_ADMIN_KEY` (or `MINDS_ADMIN_KEY`);
  `minds-smoke-teardown` also reads
  `secrets/minds/<tier>/supertokens/{SUPERTOKENS_CONNECTION_URI,SUPERTOKENS_API_KEY}`
  and `secrets/minds/<tier>/neon/DATABASE_URL`, which the stale sweep needs to
  list the tier's accounts and find the pool rows an abandoned one still holds.
  Deleting an account needs no store credentials of its own: the connector's
  two deletion steps do the removal, so the throwaway goes through
  `DELETE /account` as its holder and only ending a grace period early is an
  operator call.
- Toolchain: `uv run playwright install chromium` once (the hosted sign-in page
  and the share visit run in the suite's own headless Chromium). For the
  dev-checkout build, the Node `apps/minds/.nvmrc` pins and its pnpm on `PATH`
  (installed as `apps/minds/docs/dev-setup.md` describes; from the repo root,
  `export PATH="$HOME/.nvm/versions/node/v$(cat apps/minds/.nvmrc)/bin:$PATH"`), then
  `cd apps/minds && pnpm install --frozen-lockfile && node scripts/ensure-binaries.js && pnpm run build:ui`.
- The build under test, named by one env var: `MINDS_SMOKE_DEV_CHECKOUT=1` runs
  this checkout's Electron (the laptop default, and the only mode exercised so
  far); `MINDS_SMOKE_BINARY_PATH` names an installed executable (untried on
  macOS, where an installed Imbue Studio.app holds a single-instance lock, so
  quit the running app first); `MINDS_SMOKE_CHANNEL=alpha` downloads and unpacks
  the channel's published build: the Linux `.deb` on Linux x86_64 (what CI runs;
  needs `dpkg`), or the Apple Silicon `.zip` on an arm64 Mac (unpacked with
  `ditto`; the only Mac build the feed publishes).

The sequence, with `<tier>` staging or production and `<run-id>` lowercase
`YYYYMMDDtHHMMSSz`:

```bash
just minds-smoke-create-persistent-account <tier>                     # once per tier; writes the account's Vault leaves
just minds-smoke-setup <tier> <run-id> /tmp/smoke-<run-id>.json       # the throwaway account and its mail.tm inbox
export MINDS_SMOKE_HANDOFF_FILE=/tmp/smoke-<run-id>.json
MINDS_SMOKE_DEV_CHECKOUT=1 just minds-smoke <tier>                    # -k journey, -k signup or -k probes narrows it
just minds-smoke-teardown <tier> <run-id> --state /tmp/smoke-<run-id>.json
```

A run leases one pool row at the build's template tag, in the region the
create form already points at (and fails when the tier's pool has none to lease
there, which is a true thing to report about the tier), mints one LiteLLM key
for the persistent account, and has a real agent build the hello-world app; a
passing journey destroys its workspace and turns its share off itself. After a
failed run, `just minds-smoke-teardown` releases whatever the manifest and the
two accounts still hold, turns off their shares, deletes the throwaway account
and its mailbox, and sweeps `studio-smoke-` accounts older than a day. A fixture
whose input is missing skips and names the input. Screenshots land under
`.test_output/smoke/<test name>/`, and the run manifest at
`.test_output/smoke/run-manifest-<run id>.jsonl` (the run id from
`MINDS_SMOKE_RUN_ID`, else the handoff file, else minted when the session
starts) unless `MINDS_SMOKE_MANIFEST_PATH` names a place;
`just minds-smoke-teardown` reads that file by default. Helpers
live in `apps/minds/imbue/minds/smoke_tests/`; the tooling is
`apps/minds_admin/scripts/smoke_tests.py` (`setup`, `teardown`,
`create-persistent-account`), each with its own `just` recipe.

**Prerequisites before the first production run.** The Vault terraform change is
applied and the three GitHub Environments exist; the operator has run the
persistent-account script against production; the Slack webhook secret exists
(optional -- the workflow runs without it).

## Part 2 -- End-to-end tests worth adding

Legend for where each test best fits:

- **[snapshot]** -- fits the modal-snapshot stage (a live, stopped DEFAULT_WORKSPACE_TEMPLATE workspace
  already in Docker); add to `test_snapshot_resume.py`-style files with marks
  `minds_snapshot_resume` + `docker`. These fan out in parallel in offload and
  need no imbue_cloud login.
- **[electron]** -- needs the real Electron app driver. These now run in the
  modal-snapshot stage too (mark `minds_snapshot_resume`), reusing the snapshot
  image's baked Electron/Playwright/Xvfb toolchain; request the `xvfb_display`
  fixture so the test gets a display in the offload sandbox. See
  `test_create_workspace_and_sign_in_via_modal_then_chat_via_electron`.
- **[local]** -- a plain integration test (Flask test client or pure logic), no
  Docker; runs in the default offload suite.

All of these avoid imbue_cloud sign-in. Anything that needs a *remote* host
(Modal/AWS/Vultr) or a logged-in account is explicitly called out as
release/deployment-only and is **not** recommended for the snapshot stage.

### 2.1 Against the modal snapshot (highest leverage -- parallel, no login)

The snapshot already has a running-then-stopped DEFAULT_WORKSPACE_TEMPLATE workspace with `mngr` and a
resumable container. That makes it the cheapest place to assert real
cross-component behavior.

1. **`/api/v1/workspaces` read API against a real workspace** [snapshot] --
   resume, hit `GET /api/v1/workspaces`, assert the resumed workspace appears
   with the expected `agent_id`/`name`/`host_state`; `GET
   /workspaces/<id>` returns matching detail; `GET /workspaces/<id>/version`
   returns the `original_minds_version` label. Today these routes are only
   unit-tested with stubbed resolvers.
2. **SSH grant injection + pruning round-trip** [snapshot] -- treat the resumed
   workspace as the target. `POST /api/v1/workspaces/<id>/ssh` now returns a
   brokered `127.0.0.1:<port>` loopback endpoint (local target); assert the
   tagged `minds-ssh-grant` line lands in the target's `~/.ssh/authorized_keys`
   (via `mngr exec cat`), then re-request and confirm the same-requester line is
   refreshed (not duplicated) and any expired line is pruned. Real-world cover
   for the `compose_pruned_authorized_keys` wiring (unit-tested, but never
   exercised end-to-end over `mngr exec`).

2b. **SSH local->local broker connect** [snapshot] -- with two local workspaces
   in the snapshot (or the resumed one acting as both caller and target), call
   the `/ssh` route and then actually open an `ssh` session to the returned
   `127.0.0.1:<port>` using a keypair whose public half was submitted. Asserts
   the `SSHTunnelManager` reverse tunnel actually carries a connection to the
   target's sshd. No imbue_cloud needed -- the highest-value cover for the new
   broker.
3. **Workspace lifecycle stop/start** [snapshot] -- `POST
   /workspaces/<id>/stop` then `/start`, asserting `host_state` transitions and
   that `system_interface` serves again after start. Complements the existing
   "recovery restores a dead system_interface" test.
4. **Service discovery contract** [snapshot] -- assert the workspace writes
   `events/services/events.jsonl` with at least `system_interface` (and `web`,
   `terminal`), and that `mngr event <id> services/events.jsonl` returns them --
   the contract the desktop client relies on for byte-forwarding.
5. **Backup listing for an online workspace** [snapshot] -- if a restic repo is
   configured in the snapshot, `GET /workspaces/<id>/backups` lists snapshots and
   `is_backing_up` is a bool; otherwise assert the unconfigured shape (200 with
   an empty snapshot list and `is_configured` false). (Per-snapshot *export* is
   heavier; keep it [local] with a seeded restic repo via
   `restic_backup_a_file`.)
6. **Cross-workspace notification route** [snapshot] -- `POST
   /api/v1/agents/<id>/notifications` for the resumed workspace returns `ok` and
   lands an agent-message entry for that workspace in the notification feed.

### 2.2 Electron-driven (one more real lifecycle)

7. **Create -> v1 destroy round-trip** [electron] -- extend the existing create
   e2e: after `system_interface` renders, drive `POST
   /api/v1/workspaces/<id>/destroy`, poll `GET
   /workspaces/operations/destroy/<id>` to DONE, and assert the host is gone (the
   operator harness `scripts/electron_full_flow_e2e.py` already does a superset;
   this would crystallize the destroy half as a CI-run acceptance test).
8. **Browser create posts to `/api/v1/workspaces`** [electron] -- once the create
   UI is repointed (handoff item #2.B), assert the form submit drives the v1
   create + operation poll, not the legacy `/api/create-agent/...` routes.

### 2.3 Local integration (no Docker, no login)

9. **`require_api_or_cookie_auth` matrix** [local] -- table-driven: bearer-only,
    cookie-only, both, neither, wrong bearer -> assert 200 vs 401 across a
    representative route. Locks the dual-auth contract the whole `/api/v1`
    surface depends on.
10. **Operation-status routing precedence** [local] -- a workspace id that has
    both a stale restart record and a live destroy record resolves to the
    destroy (the documented precedence in `_handle_operation_status`); the
    `creation-` prefix routes to the creator.
11. **SSH grant validation 400s** [local] -- via the Flask test client with a
    stubbed `mngr` exec: empty/multi-line public key, whitespace in
    `requester_workspace_id`, missing `requester_workspace_id` -> 400 with the
    right message. (Requires making the route's `mngr exec` injectable; see the
    note below.)
12. **`compose_pruned_authorized_keys` over realistic files** [local] -- already
    added in `workspace_ssh_test.py`; extend with a fuzz-style case mixing user
    keys, comments, blank lines, and multiple grants to lock the
    preserve-verbatim guarantee.

### 2.4 Remote / account-bound (NOT for the snapshot stage)

These need a remote host and/or a logged-in account, so they belong in
release/deployment suites, not the snapshot stage. The release tier now has
real remote-workspace capacity (standing CI bare-metal boxes + a per-run
slice pre-bake stage -- `specs/remote-workspaces-in-ci.md`), which is where
the pool lease/create/stop-start tests run; the entries below are the
remaining ideas:

13. **SSH remote->remote establish + connect** [release] -- create two remote
    workspaces, grant SSH from one to the other, and actually `ssh`/`git pull`
    across. Exercises the implemented remote-direct path.
14. **SSH remote->local broker** [release] -- create one remote + one local
    workspace, grant SSH from the remote caller to the local target, and connect
    through the hub-brokered loopback endpoint. The broker itself is implemented;
    the local->local half can run in the snapshot stage (proposal 2b below),
    while the remote-caller half needs a cloud host so it stays release-only.
15. **imbue_cloud create + backup parity** [deployment] -- already covered
    in spirit by the `minds_deployment`/`minds_services` suites.

## Note on testability gaps

- The `/api/v1/workspaces/<id>/ssh` route shells out via `mngr exec` (through
  `_run_mngr_blocking`), which is not injectable, so success-path and 400/502
  cases can only be unit-tested after the exec call is made injectable (e.g. a
  callable on `state`). Proposals 2 and 12 depend on that small refactor; the
  pure key-composition logic is already fully unit-tested.
- The SSH **remote->local broker** (handoff #5) is implemented: a local
  Docker/Lima target's sshd is published on the hub's `127.0.0.1:<port>` (its
  host uses an SSH connector, so discovery *does* carry its endpoint), and the
  hub reverse-tunnels that into the caller's container via `SSHTunnelManager`.
  The decision logic is unit-tested (`workspace_ssh_tunnel_test.py`); the tunnel
  I/O needs a live caller+target, so its end-to-end cover belongs in the
  snapshot stage (local->local, proposal 2b) -- the remote-caller half still
  needs a cloud host and stays release-only.
