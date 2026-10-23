- Smoke tests are now a documented test category for Imbue Studio: tests that
  run after a deploy or a release, against the published artifact and the
  deployed production environment, marked `@pytest.mark.smoke` and homed in
  `apps/minds/smoke_tests/`. They never gate a release; a failure fails its CI
  job, which alerts Slack. `apps/minds/docs/testing-overview.md` gained section
  1.10 covering everything operational about them -- the target (`binary` and
  `backend` fixtures, (alpha, production) in CI), the post-deploy trigger, the
  `setup`/`test`/`teardown`/`notify_slack` job split, the two accounts, the
  production Vault keys each job holds, and `just minds-smoke <tier>` for local
  runs.

- The `minds_services` deployment test `test_logged_in_smoke` is now
  `test_logged_in_routes` (in `test_logged_in_routes.py`). It is a pre-release
  check that a shared ci env's customer-facing routes answer, which is a
  different thing from the new smoke category, so it no longer borrows the name.

- The smoke suite's shared helpers now exist at `apps/minds/imbue/minds/smoke_tests/`:
  the env-var contract and Vault layout of a smoke run (`primitives.py`), the
  frozen types it passes between its jobs (`data_types.py`, whose
  `SmokeRunHandoff` round-trips through env vars, a JSON state file, and Vault
  leaves), the resolution of the environment under test from a tier's committed
  `client.toml` and `deploy.toml` (`backend.py`), the connector and pool-DB
  calls its accounts and cleanup need (`accounts.py`, every request labelled
  `imbue-studio-smoke-test/1`), the per-run handoff's storage (`handoff.py`),
  the manifest a test records what it created in (`manifest.py`), and the
  finder for smoke accounts earlier runs left behind (`stale.py`). The suite
  itself lands separately.

- The mail.tm account lifecycle moved out of the deployment-tests orchestrator
  into `imbue/minds/deployment_tests/_mailtm.py` (`create_mailtm_account`,
  `delete_mailtm_account`), so both orchestrators create and delete a mailbox
  the same way; the caller now chooses the mailbox's local part, and a deletion
  reports whether the mailbox was still there (mail.tm answers an
  already-deleted account's JWT with a 401, which now counts as already gone).
  The SuperTokens user-listing pagination behind the stale-user sweep is now a
  shared generator, `iter_supertokens_users`.

- The smoke suite itself now exists at `apps/minds/smoke_tests/` (`@pytest.mark.smoke`,
  run with `just minds-smoke <tier>`): the journey test launches the Imbue Studio
  build under test, signs in through the app's web-login flow (completing the
  hosted page in the suite's own Chromium), creates an Imbue Cloud workspace,
  has its agent build and register a hello-world web app, shares the workspace
  from the Share tab and opens the app's route through the share as the owner,
  turns sharing off, and destroys the workspace; the signup test verifies the
  run's throwaway account's email through mail.tm and checks the download page
  lists installers; the probes cover the connector, the LLM proxy, the hosted
  accounts pages, the web chrome, the legacy redirects, the download redirect,
  and the release feed. Fixtures resolve the backend (`MINDS_SMOKE_TIER`), the
  build (`MINDS_SMOKE_CHANNEL` downloads and unpacks the channel's Linux `.deb`;
  `MINDS_SMOKE_BINARY_PATH`; `MINDS_SMOKE_DEV_CHECKOUT=1`), the persistent
  account (env or the tier's Vault leaves), the throwaway account (the handoff
  from env, the new `MINDS_SMOKE_HANDOFF_FILE`, or Vault), the run manifest, and
  the launched app; each skips with a sentence naming its missing input. The
  helper package gained `feed.py` (channel manifests), `binary.py` (build
  resolution and launch), `browser.py` (the hosted sign-in page), and
  `app_flows.py` (the journey's steps inside the app).

- `e2e_workspace_runner.electron_app_session` takes an `ElectronLaunchTarget`
  (an executable plus the main script a bare Electron needs), so the same CDP
  launch machinery drives a packaged build; a `None` template path leaves the
  create form on the app's published defaults, and a `base_env` replaces the
  environment the app would otherwise inherit from the test process.
  `wait_for_workspace_ready_or_failure` and `chat_frame` (the workspace's
  welcome chat, returned before its composer renders) are public. The
  verification-email resend loop of the signup e2e test moved into `_mailtm.py`
  as `wait_for_verification_token_with_resends`.

- The journey launches the app the way an installed one is launched, because
  the build under test is whatever the channel published and so cannot carry an
  accommodation for being driven by a test: from an empty directory outside any
  checkout, on an environment the launcher composes rather than inherits. Every
  `PYTEST_*` variable of the test process is dropped -- pytest's own marker,
  which mngr's config guard reads and which would otherwise have the app's mngr
  refuse every config it loads, and the resource guards' per-test switches,
  which would make a guarded binary the app shells out to exit 127 -- along with
  its `MNGR_*` and `MINDS_*` variables, the `UV_*` switches the mngr test
  harness sets (`UV_OFFLINE` would forbid the app's first-launch `uv sync` the
  managed CPython it downloads), the test venv, and the guard shims on `PATH`; `MINDS_DATA_HOME`, `MINDS_ROOT_NAME`,
  the tier's client config, `DISPLAY` and a no-op `BROWSER` are set explicitly.
  The no-op `BROWSER` keeps the app's web-login out of the operator's own
  browser, whose signed-in account would otherwise be handed to the app; the
  suite completes the hosted sign-in page in its own headless Chromium.

- A fresh workspace boots with no AI credentials, so the journey gives it the
  account's own: it mints the LiteLLM key through the desktop's AI-keys route
  and posts the blob to the workspace's
  `POST /api/claude-auth/submit-credentials` -- the endpoint that blob is minted
  for -- requiring the workspace to report back the proxied `imbue` auth mode,
  so a credential read as a plain Anthropic key fails there rather than at the
  first reply. The prompt then goes to a chat started from the desktop's
  launcher, which the chat app mints on the account signed in by then, once
  that chat appears in the app's own `GET /api/chats` -- a create answers as
  soon as the chat has an id and its composer renders at once, but a send
  before mngr has registered the chat's agent is refused with "Chat '<id>' not
  found". The seeded welcome chat cannot take it: a workspace that booted
  without credentials renders the no-provider notice in place of its composer,
  its page reads the account list only as it boots, and the reload that would
  render the composer loses the welcome chat's own creation, so its send is
  refused the same way.

- `MINDS_SMOKE_CHANNEL` also resolves a channel's published build on an Apple
  Silicon Mac: the `-arm64-mac.zip` the channel's `mac` manifest lists, unpacked
  with `ditto` and launched from the bundle, so `just minds-smoke` vets a channel
  from a developer laptop as well as from the Linux CI lane.

- A local run's manifest is one file per run,
  `.test_output/smoke/run-manifest-<run id>.jsonl`, which
  `just minds-smoke-teardown` reads by default, so a teardown only ever
  releases its own run's resources.

- A channel's downloaded archive is fetched inside the directory it unpacks
  into and deleted once unpacked, so its name derives from nothing and two
  builds can never share one.

- A channel's published build is cached per machine under
  `~/Library/Caches/imbue-studio-smoke/builds` (`$XDG_CACHE_HOME`, else
  `~/.cache`, on Linux) rather than under the system temp directory, which
  macOS prunes by access time: a swept bundle kept the executable the cache
  check looked for while losing the helpers Electron needs, so a run days later
  launched a build that could not start and failed as a CDP timeout.

- The cache key now carries the manifest's `sha512`, and a download is verified
  against it before being unpacked, so a channel that republishes a version
  fetches afresh instead of reusing the old tree, and a truncated or
  substituted download is refused rather than launched.

- Teardown deletes the run's throwaway account through the connector's own
  `DELETE /account`, signed in as that account, so every run exercises the
  deletion path a real user takes; ending its grace period early is the one
  operator call left. The stale sweep, which finds accounts by address and
  holds no password for them, uses the operator delete and finalize routes.
  Nothing shells out to `scripts/delete_accounts.py` any more, and the teardown
  job no longer needs LiteLLM or analytics credentials at all -- the connector
  does that removal itself.
