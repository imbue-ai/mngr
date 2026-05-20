# Next steps for the minds deployment / services test suite

State as of branch `mngr/minds-good-testing` (PR https://github.com/imbue-ai/mngr/pull/1676).
Base branch is `mngr/minds-deployment-fixups`.

## Where we are

Two of six initial tests pass against real cloud. Four remain `@pytest.mark.skip`ped pending per-test driver work. The orchestrator + fixtures + auth + cleanup plumbing is all real and exercised.

### Tests passing

- **`test_logged_in_smoke`** (`minds_services`)
  - Invoked via `just minds-test-services-against dev-josh-1 apps/minds/deployment_tests/test_logged_in_smoke.py --no-fct-push`.
  - Hits `GET /health/liveness`, `/version`, `/generation`, litellm `/health/liveness`, and auth'd `/tunnels` (with a session JWT from a freshly-created + email-verified `verified_user` against the env's SuperTokens app).
  - Verifies the `services-against` flow end-to-end: orchestrator loads `~/.minds-<env>/client.toml` + `secrets.toml`, builds `deployment_envs.json`, exports per-shared-env secret env vars, runs pytest, cleans up the mail.tm account.

- **`test_deploy_new_version`** (`minds_deployment`)
  - Invoked via `just minds-test-deployment-only apps/minds/deployment_tests/test_deploy_new_version.py`.
  - `ephemeral_env` fixture deploys a fresh `dev-ci-<stamp>-<uuid>`, the test captures v1's `deploy_id` from `/version`, redeploys via subprocess `minds env deploy`, captures v2's `deploy_id`, asserts it advanced (lex sort strictly greater). Fixture teardown unconditionally destroys the env.
  - Wall time ~3.5 min per run (2 deploys + 1 destroy).

### Tests still skipped

| Test file | Mark | Why skipped (current blocker) |
|---|---|---|
| `test_deploy_round_trip.py` | `minds_deployment` | Needs per-provider enumeration assertions (Modal env list, Neon project list, SuperTokens app list, etc.) to verify "every expected resource exists" + "every resource is gone". |
| `test_deploy_rollback.py` | `minds_deployment` | Needs the `MINDS_INJECT_BROKEN_HEALTHCHECK=1` env-var injection mechanism wired into the v2 deploy, plus version capture + post-rollback version assertion. The connector-side `MINDS_INJECT_BROKEN_HEALTHCHECK` check itself already ships in this PR. The deploy-side auto-rollback (`provisioning.deploy_env` → `_exec_into_recover` → `recover_env` → `rollback_modal_app`) is already wired up and was confirmed empirically (see "Pre-existing bugs we hit" below). |
| `test_signup_tunnel.py` | `minds_services` | Needs the realistic sign-up driver (POST to connector `/auth/signup` + mail.tm verification-token poll + POST `/auth/verify-email`), the email-one-time-code sign-in driver, in-process desktop client to create a workspace from the FCT template, the "forward system-interface" trigger (creates the Cloudflare tunnel), and Cloudflare-list assertion helper. |
| `test_litellm_via_workspace.py` | `minds_services` | Needs the in-process desktop-client workspace-create driver (`mngr create --template <fct-path>` with `AIProvider.IMBUE_CLOUD`), `mngr message` subprocess driver against the running container, and a Neon DSN query helper to assert spend lands in `litellm_cost`. Also needs the operator's local Docker daemon to be running. |

All four skipped tests already have:
- Their planned-flow documented in their docstrings (read those before un-skipping).
- A `wait_for_env_ready(shared_env("default"))` preamble call in their stub bodies so cold-boot tolerance is in place.
- The appropriate fixtures wired (`shared_env`, `signup_email`, `verified_user`, `ephemeral_env`, `fct_template_ref`).

The fixtures themselves are real implementations:
- `shared_env(role)` reads `deployment_envs_config` + per-shared-env secret env vars and returns a `SharedEnvHandle`.
- `verified_user` creates a user via SuperTokens admin API (signup → mint verification token → verify-email-with-token → connector `/auth/signin`), yields a handle with a real session JWT, deletes the user in teardown.
- `ephemeral_env` shells out to `minds env activate --create` + `minds env deploy`, parses the resulting `client.toml`, yields the handle, runs `minds env destroy` in teardown.
- `signup_email` returns a `MailtmInbox` rooted at `<account>+<uuid>@mail.tm` with `wait_for_verification_token()` / `wait_for_one_time_code()` helpers.

## Cleanup to do before adding more tests

There are three pieces of cleanup the operator flagged. Suggested order: **#2 first** (deploy strategy), since it lets us delete the test-side `_stop_running_modal_containers` helper entirely; then **#1** (HOME isolation), then **#3** (MODAL_PROFILE plumbing).

### #1 -- HOME isolation: copy `~/.vault-token` into the test's tmpdir HOME

**Current (wrong) approach**: `apps/minds/deployment_tests/conftest.py` overrides the project-wide autouse `setup_test_mngr_env` fixture with a no-op so HOME stays at the operator's real home. This works but throws away the test-isolation guarantees the autouse provides for other things (MNGR_HOST_DIR, MNGR_PREFIX, MNGR_ROOT_NAME).

**Why we did it this way**: the `setup_test_mngr_env` autouse fixture from `imbue.mngr.utils.plugin_testing.register_plugin_test_fixtures` points HOME at a per-test tmpdir for filesystem isolation. The in-test `minds env deploy` subprocess uses `vault` which reads its token from `~/.vault-token`. With HOME tmpdir'd, the token file isn't there, every Vault read returns 403, every deploy fails.

**Right approach**: keep the existing autouse fixture untouched (its isolation is generally useful). Add a step that copies `~/.vault-token` (and possibly `~/.modal.toml`) from the operator's real home into the per-test tmpdir HOME before the test runs. The `vault` CLI then finds its token at the expected `~/.vault-token` path (now pointing at the tmpdir's copy), and the rest of the isolation is preserved.

Suggested implementation:
- Revert the `setup_test_mngr_env` no-op override in `apps/minds/deployment_tests/conftest.py`.
- Add a separate autouse fixture (or extend `_sweep_stale_test_users` or similar) that, after the HOME-tmpdir autouse runs, does `shutil.copy2(Path(real_home_from_env_or_pwd) / ".vault-token", new_home_dir / ".vault-token")` and `shutil.copytree(real_home / ".modal", new_home_dir / ".modal")` (or copy the relevant file -- check what the modal CLI reads from `~/.modal.toml`).
- The "real home" needs to be captured BEFORE the autouse changes HOME -- probably at module import time or via a session-scoped fixture that runs before the function-scoped HOME setter.

Tricky bits to figure out during implementation:
- What's the canonical way to capture the operator's real home before the autouse fires? `pwd.getpwuid(os.getuid()).pw_dir` works regardless of HOME; that's probably the right call.
- Are there other dotfiles the test subprocesses need? At minimum: `.vault-token`, possibly `.modal.toml` (modal CLI auth), possibly `.gitconfig`. Check what each subprocess invocation needs.
- The same fix should work for both the `minds_services` and `minds_deployment` tests since both shell out to `minds env deploy` (verified_user fixture for services, ephemeral_env for deployment).

### #2 -- Modal deploy strategy: rollover vs recreate

**Background**: Modal recently added a `modal deploy --strategy=recreate` flag (per the Modal account contact). The default strategy is "rollover" (containers from the prior version stay alive serving traffic until they idle out, replaced by new-version containers for fresh requests). `recreate` terminates ALL running containers so every subsequent input is guaranteed to hit a new-version container. Trade-off: `recreate` has a brief downtime / cold-boot-latency window; `rollover` has a stale-content window.

**Why this matters for the test suite + real operator deploys**:

The `test_deploy_new_version` test originally failed (even after the v2 deploy completed and `MINDS_DEPLOY_ID` advanced server-side) because the warm v1 container kept serving `/version` requests until it idled -- and every poll request reset its idle timer. The fix in this PR is a test-side helper `_stop_running_modal_containers` (in `apps/minds/deployment_tests/test_deploy_new_version.py`) that runs `modal container list` + `modal container stop` after each redeploy.

The same issue affects **real operator deploys**, not just tests. An operator running `minds env deploy` twice in succession against their dev env, or running `minds env deploy` followed immediately by a desktop-client request that hits a newly-added endpoint, would see the new code's behavior not actually serving for several minutes (Modal's default warm-container idle window). This is most visible when:
- A new endpoint is added (the warm prior container 404s the new route -- we softened this in the healthcheck via the cold-boot 4xx-as-transient fix shipped earlier in this PR, but real clients hitting the new endpoint still see 404 until the warm container dies).
- An existing endpoint's behavior changes (the warm container silently returns the old shape / old data -- no error, just wrong).
- A bugfix is deployed (the operator believes they fixed it but the bug still surfaces for several minutes).

**The chosen policy**:

CLI flags (explicit override -- always win):
- `--hard` -> use `modal deploy --strategy=recreate` (terminate all running containers; downtime, but guaranteed fresh)
- `--soft` -> use `modal deploy` (default rollover; no downtime, eventual consistency)

When no flag is specified, derive the strategy from context:
- If the deploy includes a migration of any kind (DB schema, irreversible state change), use `recreate`. Even if the migration is technically backwards-compatible, leaving old code running against a new database schema risks transient, untested combinations that are easy to miss in testing.
- If no migration AND env is dev / CI / test, use `recreate`. The stakes are low and the deploy-and-immediately-observe pattern is the dominant operator flow.
- If no migration AND env is staging / production, use rollover (the current default). Production cares about zero-downtime more than fresh-container-guarantee, and the operator can opt in with `--hard` when they need it.

**Where this needs to land**:
- `apps/minds/imbue/minds/envs/per_env_deploy.py::_deploy_modal_app` -- shellout adds `--strategy=recreate` arg when selected.
- `apps/minds/imbue/minds/envs/provisioning.py::deploy_env` -- needs to know whether a migration is happening + needs to surface the strategy decision. Probably accepts a `strategy: DeployStrategy` enum from the caller.
- `apps/minds/imbue/minds/cli/env.py::env_deploy` -- adds `--hard` and `--soft` click options, resolves to a `DeployStrategy` value, threads through.
- New enum: `DeployStrategy = Literal["rollover", "recreate"]` (or `UpperCaseStrEnum`) -- probably in `provisioning.py` next to `DeployedEnv`.
- Migration detection -- need a way to know "this deploy involves a migration". Options: a flag the operator passes (`--has-migration`?), automatic detection by comparing migration files between the deployed version and HEAD, or migrations are always considered "any deploy could have one" (which makes `recreate` the default for dev even without migration detection -- aligns with policy bullet 3 anyway).

**Effect on the test suite**:
- Once the deploy CLI does the right thing, `_stop_running_modal_containers` can be deleted from `test_deploy_new_version.py`. The test just calls `minds env deploy` (which will use `recreate` for the dev env) and the next `/version` GET cold-boots the new version naturally.
- This also means the future `test_deploy_rollback` test doesn't need its own container-stop logic.

### #3 -- `MODAL_PROFILE` plumbing: use `_modal_profile_for_tier_or_none`

**Current**: `apps/minds/imbue/minds/deployment_tests/helpers.py::build_minds_env_subprocess_env` hardcodes `_DEV_MODAL_PROFILE = "minds-dev"`. `apps/minds/deployment_tests/test_deploy_new_version.py::_stop_running_modal_containers` also hardcodes `"minds-dev"`. Both are unconditionally dev-tier.

**Why hardcoded today**: speed of iteration on the first test. The dev-tier profile is correct for everything we run today, and the only call site at the time was the orchestrator's dev-env flow.

**Why it needs to change**: the spec already anticipated separating dev / staging / production profiles. The operator additionally noted that "we're going to separate so that we have a CI profile as well as dev at some point", so the hardcoded value will silently target the wrong workspace once that happens.

**Right approach**: use the existing `_modal_profile_for_tier_or_none(tier_for_env_name(name))` function from `apps/minds/imbue/minds/cli/env.py`. That helper reads the tier's `deploy.toml` (`apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`) for the `modal_workspace` field. Same logic the production `minds env activate` flow uses.

Suggested implementation:
- Move (or re-export) `_modal_profile_for_tier_or_none` from `cli/env.py` to a place importable from the test helpers (probably alongside `_tier_for_env_name` in `_activated_env.py` or a new module).
- Update `build_minds_env_subprocess_env` to take the env `name`, derive its tier, and look up the modal profile via the shared helper (falling back to no `MODAL_PROFILE` when the helper returns `None`, matching what `minds env activate` does).
- Drop the `_DEV_MODAL_PROFILE` constant.
- Update `_stop_running_modal_containers` to take the modal profile derived the same way (or accept a `name: DevEnvName` and do the derivation itself).

## Pre-existing bugs we hit (already fixed in this PR)

For context: a couple of pre-existing bugs in `mngr/minds-deployment-fixups` blocked the test work and were fixed in this PR's commits. Listing them here so reviewers know they're load-bearing fixes that should NOT be reverted even though they're outside the strict "test suite" scope:

- **Kwarg mismatch in `_stop_modal_app_for_provider` + `_rollback_modal_app_for_provider`** (`apps/minds/imbue/minds/cli/env.py`). Both took positional `cg` but `recover.py` called them with kwarg `parent_cg=`, so every healthcheck-failure auto-recover TypeError'd. Fix: rename the param to `parent_cg` in both wrappers. Commit `f69cd46cf`.
- **Healthcheck cold-boot 4xx-as-definitive too strict** (`apps/minds/imbue/minds/envs/health_check.py::_is_transient_status`). The original logic treated any 4xx as definitive immediately, meaning the first deploy of any new healthcheck path (like `/health/liveness` when it was added) would auto-rollback because Modal serves stale containers from the prior version during the swap window. Fix: also treat 4xx as transient within the first 10s cold-boot window; 5xx logic unchanged. Commit `1e0382219`.

## Branch state + open PR

- Branch: `mngr/minds-good-testing`
- Base: `mngr/minds-deployment-fixups`
- PR: https://github.com/imbue-ai/mngr/pull/1676 (draft)
- Spec: `specs/minds-deployment-tests.md`
- Changelog: `changelog/mngr-minds-good-testing.md`

Notable recent commits:
- `5e3b32d49` test_deploy_new_version passes: override HOME-isolation + stop stale containers (the HOME override here is the WRONG approach per #1 above; replace it)
- `7284ca69b` WIP: ephemeral_env helpers + test_deploy_new_version body + deployment-only recipe
- `f69cd46cf` Fix kwarg mismatch in `_stop_modal_app_for_provider` + `_rollback_modal_app_for_provider`
- `77214a358` Wire up the first end-to-end smoke against dev-josh-1
- `1e0382219` Healthcheck cold-boot tolerance + defensive cleanup in test fixtures

## How to run today

```bash
# Prereqs (one-time):
# 1. `vault login -method=oidc`  -- writes ~/.vault-token
# 2. `cd ~/project/forever-claude-template && git worktree add \
#      <monorepo>/.external_worktrees/forever-claude-template HEAD`
# 3. Have a dev env deployed at dev-josh-1 (`eval "$(uv run minds env activate dev-josh-1)" && uv run minds env deploy`)

# The two passing tests:
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200
export VAULT_NAMESPACE=admin

just minds-test-services-against dev-josh-1 \
  apps/minds/deployment_tests/test_logged_in_smoke.py --no-fct-push

just minds-test-deployment-only \
  apps/minds/deployment_tests/test_deploy_new_version.py
```

After the three cleanups above land, the next test to un-skip is operator's choice. Options ranked by complexity (lowest first):
1. `test_deploy_rollback` -- closest in shape to `test_deploy_new_version`; main new work is the `MINDS_INJECT_BROKEN_HEALTHCHECK` injection mechanism for the v2 deploy and the post-rollback version assertion.
2. `test_deploy_round_trip` -- adds the per-provider enumeration assertions (Modal env list, Neon project list, SuperTokens app list, OVH/Cloudflare tag enumeration).
3. `test_litellm_via_workspace` -- requires the in-process desktop-client workspace-create driver + `mngr message` driver + Neon DSN query helper + local Docker daemon.
4. `test_signup_tunnel` -- the biggest: full sign-up + email-verify-via-mail.tm + one-time-code sign-in + workspace creation + system-interface forwarding + Cloudflare-list assertion.
