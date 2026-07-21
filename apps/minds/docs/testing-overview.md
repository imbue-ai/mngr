# Minds testing overview

This is a map of every kind of test the minds app has, where each kind runs, and
a backlog of end-to-end (e2e) tests worth adding -- with emphasis on tests that
fit the modal-snapshot CI stage (a pre-baked workspace-in-Docker image that lets
e2e tests fan out in parallel through offload).

The test taxonomy and locations follow the repo `style_guide.md` ("Types of
tests"): unit (`*_test.py`), integration (`test_*.py`, unmarked), acceptance
(`@pytest.mark.acceptance`), and release (`@pytest.mark.release`). Minds adds a
few app-specific markers (below).

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
| `test_snapshot_resume.py` (9 tests) | each `minds_snapshot_resume` + `docker` (+ `rsync` on the electron test) + per-test `timeout` | Most assert against a Modal-snapshot sandbox (pre-baked, stopped DEFAULT_WORKSPACE_TEMPLATE workspace container): resume sanity checks, the backup-update chat gate against a live LLM-backed chat, the backup-service check/update/force-update converge loop (real supervisord + `official`-remote tag fetch from GitHub), and the backup enable / env-repair / destination-change flow (real minds-side restic provisioning + `mngr exec` injection; installs a pinned restic on the sandbox host when the image lacks the bundled one). `test_create_apikey_workspace_and_chat_via_electron` reuses the snapshot image's warm Electron/Playwright/Xvfb toolchain to drive the real Electron app: it creates a fresh local Docker DEFAULT_WORKSPACE_TEMPLATE workspace via the manual `api_key` AI provider (needs `ANTHROPIC_API_KEY`), sends a chat message, and asserts the agent replies, then `mngr destroy`s in `finally`. Shares its driver with `desktop_client/e2e_workspace_runner.py`. Only via `just test-offload-minds-snapshot` (or `just minds-test-electron` locally). See 1.5. |
| `test_sse_redirect.py::test_sse_redirect_on_done` | `release` | Werkzeug server + Playwright; verifies the creating-page SSE stream delivers `done` and the JS redirects. No Docker/agent. |
| `imbue/minds/test_claude_version_alignment.py::test_claude_code_version_matches_default_workspace_template_pin` | `release` | Checks the Claude Code CLI pin matches the DEFAULT_WORKSPACE_TEMPLATE pin. |

### 1.3 Deployment-test suites (`deployment_tests/`)

An importable helper package, excluded from all offload runs and `test-quick`;
driven only by `just minds-test-deployment` and siblings (orchestrator
`apps/minds/scripts/test_deployments.py`). Every test here carries
`@pytest.mark.release` (so it is part of the shared release suite, discoverable
by tag) in addition to its capability mark; all minds release tests run from the
minds jobs (`test-minds-release`), never from the mngr release workflow, which
excludes the whole `apps/minds` tree by path.

- `@pytest.mark.minds_deployment` (each mints its own ephemeral CI env):
  `test_deploy_new_version`, `test_deploy_auto_rollback_on_broken_healthcheck`,
  `test_deploy_then_destroy_round_trip`.
- `@pytest.mark.minds_services` (run against a pre-stood-up shared env):
  `test_logged_in_smoke`, `test_realistic_signup_verify_signin_create_tunnel_signout`
  (currently `skip`), `test_litellm_spend_tracking_via_local_workspace`
  (currently `skip`).

### 1.4 JS / Electron tests (`apps/minds/test/`, `apps/minds/frontend/`)

- **Node unit** (`test/unit/*.test.js`): `node --test` cases for the Electron
  main process (startup/surface routing, view layout, log rotation). Run via
  `pnpm test:unit`; in CI via the `test-minds-frontend` job.
- **Frontend vitest** (`frontend/src/**/*.test.ts`): strict `tsc --noEmit`
  plus vitest (jsdom) for the mithril components and the mount protocol. Run
  via `pnpm test:frontend`; in CI via the `test-minds-frontend` job.
- **Playwright e2e** (`test/e2e/`, `playwright.config.js`, `pnpm test:e2e`):
  - `macos-launch.spec.js` -- launches the installed `/Applications/Minds.app`
    via the `mindsApp` fixture. **The only e2e spec wired into CI** (in
    `minds-launch-to-msg.yml`).
  - `landing-stopped-mind-restart.spec.js` and `recovery-redirect.spec.js` --
    fast DOM-level renderer-contract tests (plain browser `page`, no
    Electron/Docker/backend; shell out to `uv` to render the real Jinja). Run
    locally only; **not in CI.**

### 1.5 CI map

`.github/workflows/ci.yml` (push to main + all PRs):

- **`check-changelog`** -- changelog gate.
- **`test-offload`** ("Unit + Integration Tests") -- `just test-offload`. Filter:
  `not acceptance and not release and not flaky and not sdk_live and not
  minds_deployment and not minds_services and not minds_snapshot_resume`, plus a
  retrying `flaky` group.
- **`test-offload-acceptance`** ("Acceptance Tests") -- `acceptance and not
  docker and not docker_sdk and not minds_deployment and not minds_services and
  not minds_snapshot_resume`; pre-creates a shared Modal env.
- **`test-docker`** -- real Docker daemon on a GitHub runner; `(docker or
  docker_sdk) and not release and not minds_snapshot_resume`.
- **`test-minds-frontend`** -- Node-only job (no Python venv): `pnpm install`,
  the esbuild bundle build (`pnpm build:js`), the frontend typecheck + vitest
  suite (`pnpm test:frontend`), and the Electron main-process node unit tests
  (`pnpm test:unit`).
- **`build-minds-snapshot` + `test-minds-snapshot`** ("Minds Snapshot Resume
  Tests") -- the modal-snapshot stage (see below). All `minds_snapshot_resume`
  tests run here, including the Electron create+chat test (which reuses the
  snapshot image's baked Electron toolchain) -- there is no longer a separate
  `test-docker-electron` job.
- **`cleanup-modal-environments`** -- sweeps old Modal test envs + leaked
  snapshot images.
- **`test-minds-release`** (manual only -- `workflow_dispatch` +
  `run_minds_release_tests`) -- the home for **all** minds release tests. Runs
  the `minds_deployment` group via the deployment orchestrator (each mints +
  destroys its own ephemeral ci env), then the plain minds `@release` tests that
  need no ci env, selected by tag: `-m 'release and not minds_deployment and not
  minds_services and not minds_snapshot_resume'`. That is where
  `test_claude_version_alignment.py`, `test_sse_redirect.py` (Chromium installed
  in-job), and `test_aws_workspace_release.py` (skips without AWS opt-in) run.

`.github/workflows/release-tests.yml` (`workflow_dispatch` + `v*` tags) -- the
*mngr* release suite only. Both jobs exclude the whole `apps/minds` tree by path
(`--ignore apps/minds`); all minds release tests run from `test-minds-release`
above (the minds release procedure is a manual dispatch, not a `v*` tag):

- **`test-mngr-release-docker`** -- `(docker or docker_sdk) and release`, with
  `--ignore apps/minds`.
- **`test-mngr-release`** -- the `release` suite with `--ignore apps/minds`,
  matrixed `[ubuntu, macos] x group 1..12` (pytest-split).

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
  one `[groups.all]` with `filters="-m 'minds_snapshot_resume'"`,
  `max_parallel=10`. The image is deleted on success. Both jobs are gated by the
  `DISABLE_MINDS_SNAPSHOT_CI` repo variable and skipped on fork PRs.
- **Currently runs:** every `minds_snapshot_resume` test in
  `test_snapshot_resume.py` (resume sanity checks, the Electron create+chat
  round-trip, the backup-update chat gate, and the backup-service
  check/update and enable/repair/destination-change flows). Run a single one
  with `just test-offload-minds-snapshot <image-id> '--filter <test_name>'`;
  mint an image id manually via `uv run python
  scripts/snapshot_minds_e2e_state.py`.

The `ANTHROPIC_API_KEY` is pulled from Vault for the test job (so the agent can
actually run), but these tests do **not** require an imbue_cloud login.

### 1.7 Fixtures available for new tests

- **Modal snapshot** (`test_snapshot_resume.py`): the `running_workspace` fixture
  (yields a `_ResumedWorkspace` after starting the snapshot's stopped containers
  + waiting for `system_interface`), plus autouse
  `_ensure_dockerd_after_snapshot_resume`. Use marks `minds_snapshot_resume` +
  `docker`.
- **Live Electron workspace** (driver, not a fixture):
  `imbue/minds/desktop_client/e2e_workspace_runner.py` --
  `create_workspace_via_electron`, `destroy_agent_best_effort`,
  `resolve_default_workspace_template_path`, `materialize_isolated_default_workspace_template`, `ensure_minds_env_defaults`,
  `find_free_port`.
- **Deployment/services** (`deployment_tests/conftest.py`): `shared_env(role)`,
  `verified_user`, `ephemeral_env`, `signup_email` (mail.tm).
- **General minds helpers** (`imbue/minds/testing.py`): `make_git_repo`,
  `init_and_commit_git_repo`,
  `stub_mngr_host_dir`, `extract_response`; `desktop_client/testing.py`
  (`restic_backup_a_file`); `utils/testing.py` (`RecordingMngrCaller`);
  `latchkey/testing.py` (`FakeLatchkeyGatewayClient`, `build_fake_gateway_client`).

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
  `test_create_apikey_workspace_and_chat_via_electron`.
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
   dispatches (assert via a recording dispatcher).
7. **Health probe** [snapshot] -- `GET /workspaces/<id>/health` returns a
   `HostHealthResponse` with a sane `dispatch_tier` for a live workspace.

### 2.2 Electron-driven (one more real lifecycle)

8. **Create -> v1 destroy round-trip** [electron] -- extend the existing create
   e2e: after `system_interface` renders, drive `POST
   /api/v1/workspaces/<id>/destroy`, poll `GET
   /workspaces/operations/destroy/<id>` to DONE, and assert the host is gone (the
   operator harness `scripts/electron_full_flow_e2e.py` already does a superset;
   this would crystallize the destroy half as a CI-run acceptance test).
9. **Browser create posts to `/api/v1/workspaces`** [electron] -- once the create
   UI is repointed (handoff item #2.B), assert the form submit drives the v1
   create + operation poll, not the legacy `/api/create-agent/...` routes.

### 2.3 Local integration (no Docker, no login)

10. **`require_api_or_cookie_auth` matrix** [local] -- table-driven: bearer-only,
    cookie-only, both, neither, wrong bearer -> assert 200 vs 401 across a
    representative route. Locks the dual-auth contract the whole `/api/v1`
    surface depends on.
11. **Operation-status routing precedence** [local] -- a workspace id that has
    both a stale restart record and a live destroy record resolves to the
    destroy (the documented precedence in `_handle_operation_status`); the
    `creation-` prefix routes to the creator.
12. **SSH grant validation 400s** [local] -- via the Flask test client with a
    stubbed `mngr` exec: empty/multi-line public key, whitespace in
    `requester_workspace_id`, missing `requester_workspace_id` -> 400 with the
    right message. (Requires making the route's `mngr exec` injectable; see the
    note below.)
13. **`compose_pruned_authorized_keys` over realistic files** [local] -- already
    added in `workspace_ssh_test.py`; extend with a fuzz-style case mixing user
    keys, comments, blank lines, and multiple grants to lock the
    preserve-verbatim guarantee.

### 2.4 Remote / account-bound (NOT for the snapshot stage)

These need a remote host and/or a logged-in account, so they belong in
release/deployment suites, not the snapshot stage, and cannot run in this
environment today:

14. **SSH remote->remote establish + connect** [release] -- create two remote
    workspaces, grant SSH from one to the other, and actually `ssh`/`git pull`
    across. Exercises the implemented remote-direct path.
15. **SSH remote->local broker** [release] -- create one remote + one local
    workspace, grant SSH from the remote caller to the local target, and connect
    through the hub-brokered loopback endpoint. The broker itself is implemented;
    the local->local half can run in the snapshot stage (proposal 2b below),
    while the remote-caller half needs a cloud host so it stays release-only.
16. **imbue_cloud create + backup/tunnel parity** [deployment] -- already covered
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
