# Unabridged Changelog - modal_proxy

Full, unedited changelog entries consolidated nightly from individual files in `libs/modal_proxy/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-19

Rewrote the README to be self-contained and user-facing, removing internal mngr references.

## 2026-06-15

Fixed a flake where looking up a Modal function immediately after deploying it could fail with `NotFoundError`. The post-deploy lookup (`DirectFunction.get_web_url`) now retries with backoff on `NotFoundError`, riding through the brief deploy-then-lookup propagation delay instead of failing immediately.

## 2026-06-10

Strengthened the modal_proxy test suite so it catches regressions in the Modal error-translation and retry boundary that previously went untested:

- `_translate_modal_error` is now exercised on all eight branches (auth, permission-denied, not-found, invalid, internal, resource-exhausted, remote, and the generic fallback), asserting the exact `ModalProxy*` type and that the original message survives translation. A swapped, dropped, or reordered `isinstance` mapping is now caught.
- The volume retry predicate now verifies that `StreamTerminatedError` and `ProtocolError` (transient connection failures) are retried, so silently dropping them from the retry set would fail.
- Added direct unit tests for `is_environment_not_found_error` covering the empty-name, no-quotes, leading-text, path-containing-"Environment", and empty-string edges, pinning the regex that drives the retry decision.
- The stream-type and file-entry-type converters now test their unsupported-value default arm, so deleting or weakening it is caught.
- The unwrap helpers now wrap genuinely-validated `Direct*` objects around real `modal.Image`/`App`/`Volume`/`Secret` instances instead of sentinel-filled `model_construct` shells, exercising real field validation.
- Raised the stale coverage floor from 35% to 75% to match the coverage CI already measures (~80%).

## 2026-06-09

## Remove Modal async-permission-propagation workaround

Modal has fixed the bug on their side where a just-created environment returned
`modal.exception.PermissionDeniedError` for several seconds (async per-user
permission propagation) before the creating user could operate on it.
Read-after-write is now immediate, so the workaround is no longer needed.

Removed from `modal_proxy`:

- The `ModalProxyPermissionDeniedError` error class (`imbue.modal_proxy.errors`).
- The `_translate_modal_error` branch that mapped
  `modal.exception.PermissionDeniedError` to it (`imbue.modal_proxy.direct`);
  permission-denied errors once again fall through to the base `ModalProxyError`.

## 2026-06-04

Retry `modal deploy` when Modal reports "The selected app is locked - probably due to a concurrent modification".

Modal serializes mutations to a single app, so two operations targeting the same app name concurrently (e.g. parallel `mngr create` against the same persistent provider app, which redeploys the snapshot/shutdown function on every create) would race and one would fail with an app-lock error. The lock is transient -- it clears as soon as the conflicting operation finishes -- so `DirectModalInterface.deploy` now classifies it as a new retryable `ModalProxyAppLockedError` and rides through it with exponential backoff. Non-lock deploy failures are still raised immediately without retry.

This also removes a frequent flake in the Modal acceptance tests, where many subprocess `mngr create` tests share one persistent app within a shared-environment offload run and deploy into it concurrently.

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

Renamed the in-memory Modal test doubles from the `Testing*` prefix to the
repo-standard `Fake*` prefix: `FakeModalInterface`, `FakeApp`, `FakeSandbox`,
`FakeVolume`, `FakeSecret`, `FakeImage`, `FakeFunction`, `FakeExecProcess`,
`FakeExecOutput`. This matches the dominant `Fake*` convention for test doubles
across the codebase, accurately describes them (working in-memory/local
implementations, not mocks/stubs), and stops pytest from trying to collect them
as test classes (the old `Testing*` names matched `python_classes = Test*`,
which produced "cannot collect test class" warnings). No behavior change.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## Translate Modal `PermissionDeniedError`

Modal recently migrated their permission system so that the per-user permission entry for a just-created environment is propagated asynchronously (typically ~3-7 seconds after `modal environment create` returns success). During that window, any operation on the new environment raises `modal.exception.PermissionDeniedError` instead of the previous `NotFoundError`.

- `imbue.modal_proxy.errors`: new `ModalProxyPermissionDeniedError`.
- `imbue.modal_proxy.direct._translate_modal_error`: maps `modal.exception.PermissionDeniedError` to the new typed error (previously it fell through to the bare `ModalProxyError`).

Bumped pinned `modal` dependency from 1.3.1 to 1.4.3, and updated `libs/modal_proxy/imbue/modal_proxy/log_utils.py` to use Modal 1.4.x's new `RichOutputManager` ABC (the private `OutputManager` API the prior implementation depended on was refactored).

## 2026-05-08

- modal_proxy: `ModalInterface.enable_output_capture` is now an abstract method. `DirectModalInterface` hooks into the Modal SDK output system; `TestingModalInterface` returns a `nullcontext`. Stacked on #1520.
