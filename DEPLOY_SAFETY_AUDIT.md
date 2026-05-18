# Deploy safety overhaul audit (PR #1671, area B)

Audit of the deploy-safety machinery introduced by the `mngr/env-testing`
Phase 1-7 work: the unified `deploy_env`, the recover-target file
lifecycle, the `MINDS_DEPLOY_ID`-pinned Modal Secret model, the Neon
snapshot/restore flow, the per-env `flock`, and the auto-exec into
recover.

Each finding has a verdict in **bold**: **CONFIRMED BUG**, **DESIGN
RISK** (works as coded but the design has a sharp edge), **MINOR**
(real but low impact), or **NOT AN ISSUE** (looked suspicious, turned
out fine).

## Sources

- Spec: `specs/minds-deploy-safety-overhaul/spec.md`
- Implementation:
  - `apps/minds/imbue/minds/envs/provisioning.py` (orchestrator —
    spec said this would be deleted; it wasn't)
  - `apps/minds/imbue/minds/envs/per_env_deploy.py` (modal helpers —
    spec said this would also be deleted; it wasn't)
  - `apps/minds/imbue/minds/envs/recover.py`
  - `apps/minds/imbue/minds/envs/secret_lifecycle.py`
  - `apps/minds/imbue/minds/envs/health_check.py`
  - `apps/minds/imbue/minds/envs/migrations.py`
  - `apps/minds/imbue/minds/envs/providers/neon_db.py`
  - `apps/minds/imbue/minds/envs/generation.py`
  - `apps/minds/imbue/minds/cli/env.py` (CLI wiring + auto-exec into
    recover)

---

## Findings

### F1. Migrations run BEFORE Neon snapshot is taken — recover can't undo a bad migration

**Verdict: CONFIRMED BUG.**

The spec ("Deploy flow") puts snapshot at step 2, migrations at step
4. The implementation reverses the order:

`provisioning.py:_deploy_env_locked`:
- L548-562: provider creation (creates_resources=true only)
- **L584-587: `apply_pool_hosts_migrations` — runs migrations**
- L593-597: capture pre-deploy app versions
- L608-624: resolve Neon project + branch
- **L626-638: create Neon snapshot branch**
- L644-657: write recover-target file

If a migration succeeds but a later step fails (Modal Secret push,
modal deploy, health check), `recover_env` restores Neon from the
snapshot — which was taken **after** the migration ran. The migration's
effect is in the snapshot, so the restore is a no-op for the migration.
The `schema_migrations` row is in the snapshot too, so on the next
deploy the runner sees the migration as already applied and skips it.

Worse: if the migration itself fails, there's no recover-target file
yet (snapshot creation and file write both happen later), and any
partial migration changes are stuck. For shared tiers
(`creates_resources=false`) this DB is operator-managed and likely
has live traffic — a bad migration applied directly with no
rollback path is the exact failure mode the safety overhaul was
designed to prevent.

**Fix:** move the snapshot-creation block (L608-638) before
`apply_pool_hosts_migrations` (L584-587), and write the recover-target
file before migrations too. Spec order is the right one.

---

### F2. `verify_neon_token_has_restore_scope` preflight is declared but never called

**Verdict: CONFIRMED BUG (or at least: dead spec compliance).**

`Providers.verify_neon_token_has_restore_scope` is on the bundle
(`provisioning.py:308`) and is wired to the real implementation in
`cli/env.py:320`. But `grep -n providers.verify_neon` against
`provisioning.py` returns **zero matches**. The check exists, the
wiring exists, the spec requires it as a preflight ("Neon API token
has snapshot + restore-point scope"), and nothing calls it.

A token without restore scope would only fail at recover time, when
the operator runs `minds env recover` after a deploy failure. The
whole point of preflight is to catch this before any mutation.

**Fix:** in `_deploy_env_locked`, call
`providers.verify_neon_token_has_restore_scope(neon_project_id, credentials.neon_api_token)`
right after `find_monorepo_root()` and before the deploy-id mint, so
it runs at zero-cost preflight time.

---

### F3. Step 1 (provider creation) has no rollback if a sub-step fails partway through

**Verdict: DESIGN RISK.**

`_deploy_env_locked` step 1 runs three creates sequentially:
1. `providers.ensure_modal_env(...)` (idempotent — adopts pre-existing)
2. `providers.create_neon_project(...)` (lookup-first, self-cleans the
   Neon project on internal failure via the try/except in neon_db.py)
3. `providers.create_supertokens_app(...)`

If Modal env succeeds, Neon project succeeds, SuperTokens app **fails**:
no recover-target file has been written yet (it's written later, after
the snapshot at L657), the deploy raises, the auto-exec into recover
won't fire (because `recover_target_exists()` returns False), and the
operator sees the raw error with no guidance. The Modal env and Neon
project are orphaned.

`create_neon_project` has its own internal cleanup for failures inside
the project-create transaction, but a downstream failure (SuperTokens
in this case) doesn't get rolled back.

For dev tiers a leaked Modal env is mostly harmless (Modal envs are
free; the operator can re-run deploy and the env is adopted). A leaked
Neon project is more annoying (counts against the Neon org quota; the
next deploy's `_select_one_or_raise_multi_match` will refuse loudly if
it sees an orphan).

**Fix options:** (1) wrap step 1 in its own try/except that does
best-effort rollback before re-raising; (2) move step 1 to AFTER the
recover-target file write — which requires the recover-target to know
how to delete the Modal env / Neon project / SuperTokens app in
addition to its current reversal steps.

---

### F4. Snapshot branch creation + recover-target file write are not atomic with each other

**Verdict: DESIGN RISK (rare in practice).**

`_deploy_env_locked`:
- L633-638: snapshot branch created (Neon API call succeeds)
- L657: `write_recover_target_atomic(...)` (local file write)

If the Neon API call succeeds but the local file write fails (disk
full, permission denied, ENOSPC, fsync error), the snapshot branch
exists in Neon but the operator has no `.minds-deploy-recover-target-*`
file pointing at it. Recover can't run (refuses on missing file). The
snapshot branch leaks.

Symmetric concern: the deploy raises before getting to the next step,
and there's no captured target to recover from. The operator's only
indication that something happened is the failed snapshot-create log
line + the orphan branch in the Neon console.

**Fix:** wrap snapshot creation + file write in a try/except that
deletes the snapshot branch on file-write failure before re-raising.

---

### F5. Auto-exec into recover loses the operator's CLI flags

**Verdict: MINOR.**

`cli/env.py:_exec_into_recover` does `os.execvp(argv0, [argv0, "env",
"recover"])`. Any flags the operator passed to the original `minds
env deploy` invocation (e.g. `--output-format=json`) are dropped from
the recover invocation.

Today `minds env recover` takes no flags, so this is harmless. But if
recover ever gains a flag (e.g. `--skip-neon-restore`), the auto-exec
silently strips it. The next operator to add a recover flag will
forget about this code path.

**Fix:** either thread original flags through, or assert that
`env_recover` has no parameters at import time.

---

### F6. `_exec_into_recover` uses 5x `time.sleep(1)` instead of `threading.Event().wait`

**Verdict: MINOR.**

`cli/env.py:485` uses `time.sleep(1)` inside a 5-iteration countdown
loop. The repo has a `PREVENT_TIME_SLEEP` ratchet
(`test_prevent_time_sleep` in `test_ratchets.py`) that this should be
firing on. Either the ratchet count was bumped to absorb it, or it's
exempt. Either way, the project's standard pattern is
`threading.Event().wait(timeout=delay)` (see e.g.
`apps/minds/imbue/minds/cli/run.py:_sleep_then_open`).

Functionally fine — the loop runs in the main thread with no other
work to do — but inconsistent with project style.

---

### F7. `recover_env` and `_recover_env_locked` have untyped `providers` / `credentials` parameters

**Verdict: MINOR (style).**

`recover.py:276-282` and `:320-327` both take `providers` and
`credentials` without type annotations:

```python
def recover_env(
    *,
    repo_root: Path,
    env_name: str,
    providers,
    credentials,
    parent_cg: ConcurrencyGroup,
) -> None:
```

Likely a circular-import dodge (Providers / ProviderCredentials live
in `provisioning.py`, which imports from `recover.py`). The style
guide says to use the type system — this is a hole.

Also: `recover.py:468` does
```python
assert SecretStr is not None
```
with a comment explaining "this anchors the SecretStr import so the
linter doesn't strip it." That's a code smell — the proper fix is to
break the circular import (move `Providers` / `ProviderCredentials`
into their own module per the spec's "Phase 5" design that never
landed), then add real type hints.

---

### F8. Recover doesn't undo migrations, only schema-restoring them via the snapshot

**Verdict: WORKS AS INTENDED, but worth flagging.**

This is a consequence of F1: with snapshot AFTER migrations, recover
restores the post-migration state. If F1 is fixed (snapshot before
migrations), recover would restore the pre-migration state — and the
runner would then re-apply the migration on the next deploy (because
the `schema_migrations` row was rolled back). That's the right
semantics.

Today: a migration that introduces a bug (e.g., drops a column the
deployed app still reads) commits to the DB before snapshot, so
recover leaves the bug in place. The operator has to manually fix the
DB or run a corrective migration.

---

### F9. Shared-tier deploys snapshot the **operator-managed** DB and restore it on recover

**Verdict: DESIGN RISK.**

For shared tiers (staging / production), `creates_resources=false`
means the Neon project is operator-managed. The deploy still snapshots
the default branch BEFORE pushing secrets / running migrations / modal
deploy, and `recover_env` will issue
`POST /projects/{id}/branches/{main}/restore` with `source_branch_id=<snapshot>`
on failure.

If production has live traffic (other apps / users writing rows
between snapshot and recover), recover will silently clobber that
traffic's writes. The `preserve_under_name=pre-rollback-<deploy_id>`
keeps the broken state for forensics, but the parent branch is
rewound. For production this is a non-trivial blast radius.

The spec acknowledges this implicitly ("Both DBs on the target branch
[...] come back to that exact state atomically") without flagging it
as a risk for shared tiers. The implementation refuses to ship a
shared-tier deploy without `NEON_PROJECT_ID` in Vault (good — F17/F25
in the prior batch), so the operator must opt in by populating that
key.

**No fix needed** if the team has accepted that shared-tier deploys
are quiesce-before-deploy. Worth documenting prominently.

---

### F10. `make_deploy_id` is second-resolution; two simultaneous deploys could collide

**Verdict: NOT AN ISSUE (well-mitigated).**

Spec flagged this as an open question. The per-env `flock`
(`hold_deploy_lock`) serializes two deploys against the SAME env, so
they can't collide on the same deploy_id (the second waits for the
first to finish).

For two deploys against DIFFERENT envs in the same UTC second:
- Modal Secrets are scoped per Modal env, so same name in two envs is
  fine (different envs).
- Recover-target files are per-env (`-<env_name>` suffix), so no file
  collision.
- Neon snapshot branches are per-project (per-env), no collision.

The only collision risk is if two devs both target the same SHARED
Modal env (staging / production) within the same second. That would
require concurrent `--yes-i-mean-staging` invocations from two
operators — which is procedurally improbable but technically possible.
Worth a one-line comment in `make_deploy_id` acknowledging this.

---

### F11. Modal CLI error-string matching is fragile

**Verdict: MINOR (already partially mitigated).**

`per_env_deploy.get_modal_app_latest_version` (L597-603) matches
against `"could not find"`, `"not found"`, `"no such"`, `"does not
exist"`. `stop_modal_app` (L400-404) matches `"not found"`, `"already
stopped"`, `"no such"`. `delete_modal_secret` (L362-366) matches `"not
found"`, `"no such"`, `"does not exist"`.

Modal's CLI error messages aren't part of a stable public contract.
If Modal changes the phrasing (e.g., "Application 'foo' does not
exist in this environment"), the substring still matches. If they
change to "ERR_APP_NOT_FOUND" they won't. Each code path independently
guesses.

**Fix (long-term):** use Modal's structured exit codes if/when
available, or use the Modal Python SDK's typed exceptions instead of
shelling out.

---

### F12. Health check polls sequentially, not in parallel

**Verdict: MINOR.**

`health_check.py:await_apps_healthy` polls connector first to
completion, then litellm-proxy. Each gets its own 60s budget. If both
were polled in parallel, the total wall-clock time would be `max(60s,
60s)=60s` instead of `60+60=120s` worst case.

Functionally correct (the cold-boot window is reset for each app via
`start=monotonic()` capture at `_HealthPollCallable` instantiation),
just slower than necessary. For the happy path both come back within
a few seconds so the difference is invisible. For a degraded
connector cold-start (50s) the second app's full 60s starts from a
fresh clock.

Worth doing for ergonomics if a deploy has to fail fast — not blocking.

---

### F13. `_assert_deploy_url_matches` raises AFTER the URL has been deployed

**Verdict: NOT AN ISSUE in the current flow.**

By the time this assertion runs, `modal deploy` has already published
the app under the URL Modal returned. The assertion catches "Modal
returned a URL that doesn't match what we predicted."

The Modal Secret that the operator's per-env config points at carries
the **expected** URL (from `_compute_secret_overrides`), so a mismatch
here means the deployed app is at one URL but the connector is wired
to send traffic to a different URL. That's a real bug surface.

The assertion raises `ModalDeployError`, which is a `MindError`, so
the auto-exec into recover fires and rolls the app back to the
pre-deploy version. The Modal Secret then matches the pre-deploy
URL (since recover deletes the new-deploy secret as part of step 3).

Good — the safety net works.

---

### F14. `delete_neon_branch` is called on the happy-path snapshot without holding the recover-target file as backup

**Verdict: NOT AN ISSUE.**

`_deploy_env_locked` L802-814 deletes the snapshot branch after a
successful deploy. If that delete fails, the snapshot branch leaks —
but the deploy itself succeeded. Logged as a warning; operator can
delete via the Neon console.

The `delete_recover_target` call (L816) runs after the snapshot
delete, so if Neon API is flapping, the recover-target file might
still be on disk but the snapshot branch is gone. Subsequent operator
tries to run `minds env recover` would fail at the Neon restore step
(snapshot not found).

But the deploy already succeeded, so there's nothing to recover. The
file just needs to be deleted manually. Logs would point at the
mismatch.

Slightly clunky but not actually broken.

---

### F15. Recover doesn't roll back the local `client.toml` / `secrets.toml` for dev tier

**Verdict: DESIGN RISK.**

For `creates_resources=true` (dev) deploys, step 6b writes
`~/.minds-<env>/client.toml` + `secrets.toml` with the new URLs +
DSNs. Recover (`_recover_env_locked`) doesn't touch the local files.

If the deploy fails AT or AFTER the local-file write (after Modal
deploy, after health check), recover rolls back Modal + Neon to their
pre-deploy state but the local files still carry the new URLs. The
operator's next `minds run` reads the local files and tries to
connect to URLs whose backing apps have been rolled back to old
versions — but those old apps are listening at the OLD per-env URL
(per-env Modal app names contain the env name, so the URL is stable
per-env; just the secret bundle attached changes).

Wait — actually the URLs ARE stable per-env (the dev tier uses
PER_ENV strategy, so the URL is `https://<workspace>-<envname>--rsc-dev-api.modal.run`).
Recover doesn't change the URL, only the attached Modal Secrets. So
the URL written to local `client.toml` is still valid after recover.

**Re-verdict: NOT AN ISSUE for URL fields**. But DSNs in
`secrets.toml` are also valid post-recover (Neon DSNs don't change
across rollback). OK, this is fine.

---

### F16. Snapshot branch's lazy COW semantics: writes during deploy diverge billing

**Verdict: MINOR.**

Neon snapshot branches are lazy + copy-on-write. The snapshot itself
is cheap until writes diverge between snapshot and parent. Every
write the deploy does (migrations, app writes) diverges the parent
from the snapshot, costing storage.

For a normal deploy this is small (migrations + a few KB of app
writes). For a long-running deploy (e.g., a slow migration), the
divergence grows. Successful deploy deletes the snapshot
(`delete_neon_branch`), reclaiming the divergence cost.

Failed deploys leave the snapshot. Long-running failed deploys with
heavy writes could rack up Neon storage. Not a correctness bug.

---

### F17. The `tier` field in the recover-target file is unvalidated

**Verdict: NOT AN ISSUE.**

`RecoverTarget.tier` is a plain `str`. There's no validation that it
matches a real tier or any pattern. But the tier comes from the same
`deploy_config.lifecycle` the deploy was using, so recover's tier
matches deploy's tier by construction. The freeform string can't
diverge.

---

### F18. The `hold_deploy_lock` file is created mode 0o644 (world-readable empty file)

**Verdict: NOT AN ISSUE.**

`recover.py:209` does `os.open(lock_path, os.O_RDWR | os.O_CREAT,
0o644)`. The file holds no content (it's just a flock target), so
"world-readable" is meaningless. Persists across runs to avoid
recreate overhead. Fine.

---

### F19. Concurrent `minds env list` could see a half-written recover-target file

**Verdict: NOT AN ISSUE.**

`write_recover_target_atomic` uses tempfile + rename. The `os.replace`
is atomic on POSIX. A concurrent `find_all_recover_target_files`
(which uses `glob`) sees either the old state (no file) or the new
state (full file). No half-written file is visible at the final path.

---

### F20. The auto-exec countdown can be interrupted by Ctrl-C, but the recover-target file stays

**Verdict: NOT AN ISSUE (intentional).**

Per the comment at `cli/env.py:471-474`: "Ctrl-C during the countdown
leaves the recover-target file on disk so the next deploy refuses +
the operator can decide whether to run recover manually or delete the
file." Intentional and documented.

---

### F21. `apply_pool_hosts_migrations` recorded version uses a non-parameterized SQL string

**Verdict: NOT AN ISSUE (defended).**

`migrations.py:_record_applied_version` builds the INSERT via f-string
with manual single-quote escaping:
```python
safe_version = version.replace("'", "''")
sql = f"INSERT INTO schema_migrations (version) VALUES ('{safe_version}') ON CONFLICT (version) DO NOTHING"
```

`version` is a filename from `sorted(migrations_dir.glob("*.sql"))`,
so it's operator-controlled at PR review time and can't contain
injection material in practice. The defensive escape closes the
remaining gap. Fine — could be stronger with bound parameters but
that requires switching off the psql CLI shellout.

---

### F22. Empty `Vault values` for a service get a placeholder `MNGR_PLACEHOLDER=unpopulated`

**Verdict: NOT AN ISSUE (intentional).**

`per_env_deploy.py:228-231` injects `{MNGR_PLACEHOLDER: unpopulated}`
when a Vault entry returns no values. The Modal Secret create
requires at least one KEY=VALUE pair. The placeholder lets `modal
deploy` succeed; routes that consume the secret 500 at request time.

Good for the "operator hasn't populated Cloudflare yet" first-deploy
case. Could be confusing if the operator forgets to populate a
service after first deploy and the apps silently return 500 on those
routes.

---

### F23. `MINDS_DEPLOY_ID` is timezone-aware UTC but tests pass naive datetimes

**Verdict: NOT AN ISSUE.**

`secret_lifecycle.py:make_deploy_id` raises `InvalidDeployIdError` if
`now.tzinfo is None`. Naive datetimes fail loudly. Tests must pass
`datetime.now(timezone.utc)` or similar. Good.

---

### F24. `_modal_subprocess_env()` passes the entire parent env to every Modal subprocess

**Verdict: NOT AN ISSUE.**

`per_env_deploy.py:521-522` returns `dict(os.environ)` verbatim. Any
env var the operator has set (including potentially sensitive ones)
gets passed to Modal subprocesses. Standard behavior; matches how
`subprocess.Popen` works without `env=`. The Modal CLI is trusted in
this context.

---

### F25. `recover.py`'s `_cleanup_orphan_secrets` enumerates Modal Secrets by listing the whole env

**Verdict: MINOR (works but quadratic).**

`_cleanup_orphan_secrets` lists every Modal Secret in the env and
filters by suffix-match on `-<tier>-<deploy_id>`. For an env with
hundreds of secrets (e.g., dev envs with many test deploys not yet
GCed), this is a full scan. Not a correctness issue — just slower
than necessary.

The deploy-side GC uses the same pattern (`gc_old_per_tier_secrets`).
Same shape.

---

### F26. Recover's per-step error handling collects errors but doesn't surface them in order

**Verdict: NOT AN ISSUE (cosmetic).**

`_recover_env_locked` builds `errors: list[str]` and raises
`RecoverFailedError` with a multi-line concatenation at the end. The
order is: app rollbacks (in `app_versions_to_restore.items()` order),
then Neon, then secrets. Dict iteration order in Python 3.7+ is
insertion order, so app rollback errors come in the order the recover
target captured them. Fine.

---

### F27. `_resolve_host_pool_dsn_for_migrations` hardcodes `DATABASE_URL` as the Vault key for shared-tier DSN

**Verdict: MINOR.**

`provisioning.py:890` reads `neon_vault_values.get("DATABASE_URL",
"")`. If the operator named their key differently in the
`secrets/minds/<tier>/neon` Vault entry, the migration step would
raise the F17 error ("missing DATABASE_URL") — which has a clear
message but might surprise an operator who named their DSN field
`POSTGRES_URL` or `NEON_HOST_POOL_DSN`. The `.minds/template/neon.sh`
file should document the required key name.

---

### F28. Spec said `provisioning.py` and `per_env_deploy.py` would be deleted; both still exist

**Verdict: MINOR (planning artifact).**

Spec §"Files to modify" says:
- `apps/minds/imbue/minds/envs/provisioning.py` — delete entirely
- `apps/minds/imbue/minds/envs/per_env_deploy.py` — delete entirely

Both still exist (1349 + 693 lines respectively). The new `deploy.py`,
`destroy.py`, `lifecycle.py`, `preflight.py`, `repo_layout.py` modules
called out in the spec also don't exist; their content lives in the
existing files. Not a correctness bug — but the spec is now out of
sync with reality and future readers will be confused. The team
should update the spec to match what shipped.

---

### F29. Spec's `test_deploy_and_recover.py` integration test doesn't exist

**Verdict: COVERAGE GAP.**

Spec calls for a comprehensive integration test exercising:
- Happy path
- Failure during secret push
- Failure during migration
- Failure during modal deploy
- Failure during health check
- First-ever deploy failure (null `app_versions_to_restore`)
- Recover re-runnability

None of these exist. `recover_test.py` covers serialization +
file-IO; the actual recover orchestration logic
(`_recover_env_locked`, `_cleanup_orphan_secrets`, `_restore_neon`)
has no end-to-end coverage.

The whole point of the safety overhaul is that recover works when
called. Without an integration test, "recover works" is an
unverified claim.

---

### F30. Modal `app rollback` env-var preservation: spec's "critical assumption" is unverified

**Verdict: CRITICAL ASSUMPTION (not validated).**

From the spec: *"deploy → re-deploy with a different MINDS_DEPLOY_ID
→ modal app rollback rsc-dev v<prior> → modal app describe rsc-dev
→ confirm MINDS_DEPLOY_ID is back to the prior value. **This is the
critical assumption underpinning the rollback design — if Modal does
not preserve env vars across rollback, escalate before continuing
Phase 5+.**"*

The implementation went through with Phase 5+. I found no evidence in
the code, tests, or commit messages that this manual smoke test was
actually run. `rollback_modal_app`'s docstring asserts the behavior
matter-of-factly ("Re-deploys the version that was active at version,
including the env vars (notably MINDS_DEPLOY_ID) captured at that
deploy time") but doesn't link to a test or smoke result.

If Modal doesn't preserve env vars across rollback, the entire recover
flow is broken: rolled-back apps would carry the NEW `MINDS_DEPLOY_ID`
env var and try to attach to the new secret bundle — which recover's
step 3 then deletes. Every rolled-back app would 500 on next request.

**Action:** validate this assumption manually (or with a Modal
integration test) before relying on recover in any tier with live
traffic.

---

### F31. Recover-target file's `app_versions_to_restore` order is dict-iteration order

**Verdict: NOT AN ISSUE.**

Python 3.7+ preserves dict insertion order. `_deploy_env_locked` L593
builds the dict in a fixed tuple order (`(f"llm-{tier}", f"rsc-{tier}")`).
Serialization round-trips that order via JSON. Recover iterates in
the same order. Fine.

---

### F32. The recover-target file is `.minds-deploy-recover-target-<env_name>.json` but the global glob is `.minds-deploy-recover-target-*.json`

**Verdict: NOT AN ISSUE (but worth a sanity test).**

`recover.py:65` defines `_RECOVER_TARGET_GLOB = f"{_RECOVER_TARGET_PREFIX}*{_RECOVER_TARGET_SUFFIX}"`
= `.minds-deploy-recover-target-*.json`. The lock files use a
DIFFERENT prefix (`.minds-deploy-lock-`) + suffix (`.lock`), so the
glob can't accidentally match them. Good.

Tests pin the per-env file naming
(`test_find_all_recover_target_files_returns_sorted_per_env_files`).

---

### F33. No SIGTERM / SIGINT handler in `_deploy_env_locked`; partial state on Ctrl-C mid-deploy

**Verdict: DESIGN RISK.**

If the operator Ctrl-Cs during `_deploy_env_locked` (e.g., during the
~30s `modal deploy`), Python raises `KeyboardInterrupt` which
propagates up. The `with hold_deploy_lock(...)` context manager runs
its `finally` block releasing the flock. But the recover-target file
is wherever it was when the interrupt fired:
- Before snapshot: no file, partial provider creation (F3).
- After file write, before completion: file present, recover can run.

The CLI's outer `except MindError` doesn't catch `KeyboardInterrupt`
(it inherits from `BaseException`, not `Exception`). So
`_exec_into_recover` doesn't fire on Ctrl-C. Operator sees raw
KeyboardInterrupt traceback + has to manually run `minds env recover`
on the next `minds env activate`.

Acceptable for an interactive flow — Ctrl-C usually means "I know
what I'm doing." Worth documenting in the recover docs.

---

### F34. The Neon API `_neon_request` polls on HTTP 423 with no jitter

**Verdict: NOT AN ISSUE.**

`neon_db.py:175-179` uses `poll_for_value` with a fixed 2s interval
and a 120s budget. If multiple concurrent Neon API requests both hit
423, they'd retry in lockstep — but Neon's per-project locking is
strict serialization not contention-sensitive, so jitter wouldn't
help. Fine.

---

### F35. Snapshot creation uses `make_neon_snapshot_branch_name(deploy_id)` = `pre-deploy-<deploy_id>`; recover's "preserve" branch uses `pre-rollback-<deploy_id>`

**Verdict: NOT AN ISSUE (good naming).**

The two naming patterns are distinguishable. A Neon project that
accumulates multiple pre-deploy + pre-rollback branches across many
deploys (because deletes failed or the operator never cleaned them
up) is easy to grep through in the console.

---

### F36. `_RECOVER_TARGET_PREFIX` is at the monorepo root; an operator with a misconfigured `repo_root` could leak files anywhere

**Verdict: NOT AN ISSUE.**

`find_monorepo_root` walks up from CWD looking for an `apps/` marker.
The found root is what `recover_target_path` uses. If the operator
runs `minds env deploy` from a CWD outside the monorepo,
`NotInMonorepoError` raises before any file write. Inside the
monorepo, the file lands at the actual repo root. Fine.

---

### F37. Recover doesn't undo the `tracks_generation=true` Vault generation-id mint

**Verdict: NOT AN ISSUE (intentional).**

Generation id is minted by `ensure_generation_id` only if MISSING.
Re-running deploy doesn't re-mint. Recover doesn't delete the
generation entry (only destroy does). So a failed tier deploy that
made it past `ensure_generation_id` leaves the generation id in
Vault — which is fine, because the next deploy of the same tier
re-uses it.

---

## Summary

| Finding | Verdict | Action |
|---|---|---|
| F1: migrations before snapshot | **CONFIRMED BUG** | Reorder: snapshot + recover-target before migrations |
| F2: `verify_neon_token_has_restore_scope` never called | **CONFIRMED BUG** | Wire into preflight |
| F3: step-1 partial-failure leaks Modal env / SuperTokens app | **DESIGN RISK** | Add cleanup or move under recover-target |
| F4: snapshot + recover-target file write not atomic | **DESIGN RISK** | Wrap, delete snapshot on file-write failure |
| F5: auto-exec into recover loses CLI flags | **MINOR** | Document or assert no flags |
| F6: `time.sleep(1)` x5 | **MINOR** | Switch to `Event().wait` |
| F7: untyped `recover_env` params + `assert SecretStr is not None` | **MINOR (style)** | Break circular import, add types |
| F8: bad migrations not undone | **(consequence of F1)** | Fix F1 |
| F9: shared-tier snapshot + restore clobbers concurrent writes | **DESIGN RISK** | Document quiesce-before-deploy expectation |
| F10: deploy_id second-resolution collisions | **NOT AN ISSUE** | One-line comment |
| F11: Modal CLI string matching | **MINOR** | Long-term: structured exit codes |
| F12: sequential health check | **MINOR** | Parallelize if perf matters |
| F13: URL match assertion timing | **NOT AN ISSUE** | — |
| F14: snapshot delete after recover-target delete | **NOT AN ISSUE** | — |
| F15: local files not rolled back | **NOT AN ISSUE** | — (re-verified mid-doc) |
| F16: snapshot COW divergence cost | **MINOR** | — |
| F17: `tier` field unvalidated | **NOT AN ISSUE** | — |
| F18: lock file mode 0o644 | **NOT AN ISSUE** | — |
| F19: half-written recover-target during list | **NOT AN ISSUE** | — |
| F20: Ctrl-C during countdown | **NOT AN ISSUE (intentional)** | — |
| F21: SQL injection in version recording | **NOT AN ISSUE (defended)** | — |
| F22: placeholder values for missing Vault | **NOT AN ISSUE (intentional)** | — |
| F23: deploy_id requires UTC tz | **NOT AN ISSUE** | — |
| F24: full env passed to Modal subprocesses | **NOT AN ISSUE** | — |
| F25: full Modal-secret list for orphan cleanup | **MINOR** | — |
| F26: recover error-message ordering | **NOT AN ISSUE** | — |
| F27: hardcoded `DATABASE_URL` Vault key | **MINOR** | Document in `.minds/template/neon.sh` |
| F28: spec says delete provisioning.py / per_env_deploy.py | **MINOR (planning)** | Update spec to match implementation |
| F29: no `test_deploy_and_recover.py` integration test | **COVERAGE GAP** | Build one |
| F30: Modal env-var preservation across rollback unverified | **CRITICAL ASSUMPTION** | Smoke-test manually before trusting recover in prod |
| F31: app_versions_to_restore ordering | **NOT AN ISSUE** | — |
| F32: recover-target glob vs lock-file glob | **NOT AN ISSUE** | — |
| F33: KeyboardInterrupt during deploy | **DESIGN RISK** | Document |
| F34: Neon 423 poll without jitter | **NOT AN ISSUE** | — |
| F35: snapshot vs preserve branch naming | **NOT AN ISSUE** | — |
| F36: monorepo-root scoping | **NOT AN ISSUE** | — |
| F37: generation id not rolled back | **NOT AN ISSUE (intentional)** | — |

### Items I'd fix before any production deploy

In rough priority order:

1. **F1** (migrations before snapshot) — corrupts the rollback guarantee
2. **F30** (Modal env-var preservation) — the rollback design's central assumption
3. **F2** (preflight not wired) — easy fix, catches a class of failures before mutation
4. **F29** (no integration test for recover) — without this, the safety net is uncertified
5. **F4** (snapshot + recover-target file atomicity) — small fix, closes a leak
6. **F3** (step-1 partial-failure cleanup) — needs more design thought
7. **F9** (shared-tier blast radius) — documentation + procedural fix

Everything else is style, performance, or "would-be-nice".
