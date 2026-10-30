# Unabridged Changelog - modal_proxy

Full, unedited changelog entries consolidated nightly from individual files in `libs/modal_proxy/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-17

`DirectModalInterface` accepts optional `token_id` / `token_secret`; when set, every SDK call authenticates through a client built from those credentials (`client=`), and the `modal` CLI subprocesses (environment create, deploy) receive the tokens through their own environment without mutating the process environment. Unset, the SDK's own credential resolution applies unchanged.

Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entry `mngr-per-client-host-keys.md` in this directory belongs to the same PR.

- The `modal deploy` subprocess environment carries both main's extra env and the instance's own Modal tokens.

- Review fixes on top of the merge: `deploy()` carries its `extra_env` into the `modal deploy` subprocess again (the route deployer's `MNGR_MODAL_SOURCE_MARKER_NAME` had been dropped by the merge), and `is_function_deployed` authenticates with the instance's own client like every other SDK call.

## 2026-10-14

A volume read or write now waits out a server error from the storage behind the volume instead of failing the caller's whole command (MIND-479).

A Modal volume's blocks live on object storage that Modal reaches over plain HTTP, separately from its control plane. One helper in the Modal SDK turns every failed HTTP status from that storage into an exception: `503` becomes a `ServiceError`, and everything else -- including `500`, `502` and `504` -- becomes an `ExecutionError`, the class Modal otherwise raises for programming errors. `_translate_modal_error` had no branch for `ExecutionError`, so a storage-side `500` arrived as a plain `ModalProxyError`, which is not transient, and the 60s retry already wrapped around `read_file` and `write_files` never re-issued the call. On 2026-10-07 and again on 2026-10-09 that storage answered `500` with a Cloudflare error page, and reading a host record failed on the first blip -- failing `mngr create`, `mngr destroy` and the Modal acceptance suite across six branches and `main`.

Added: `ModalProxyBlockStorageError`, a `ModalProxyTransientError`, raised when a Modal error message reports a 5xx from block storage. A 4xx is left exactly as it was: `Request failed with status 404 Not Found: block not found` is the storage answering rather than failing, and retrying it for 60s would only delay that answer.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

`FakeImage.dockerfile_commands` now records the command batches it is given, as `applied_command_batches`, so a test can assert what each simulated layer was built from rather than only that an image came back.

`ImageInterface.dockerfile_commands` takes `build_args`, the values for the ARG instructions its commands declare, and forwards them to Modal. `FakeImage` records them as `applied_build_args`.

## 2026-10-12

Fixed: a `modal deploy` that Modal failed of its own accord is now reported as something a caller can retry, instead of as a terminal error (MIND-452).

- `DirectModalInterface.deploy` classifies "Worker disappeared, image build failed" -- Modal's image builder losing the worker a build was running on -- as `ModalProxyImageBuilderLostError`. A build that failed on its own contents, such as a command in the image exiting non-zero, is unaffected and still terminal.

- A deploy that outruns the 180s one attempt is given now raises `ModalProxyDeployTimeoutError` instead of letting `subprocess.TimeoutExpired` escape. That was also a hole in this package's promise that callers only ever see `ModalProxy*` errors, since no caller can catch a type it is never told about.

- New `ModalProxyDeployRetryableError` is the single statement of which deploy failures warrant another attempt. `ModalProxyAppLockedError` now subclasses it, so `except ModalProxyAppLockedError` keeps behaving as before.

- `DirectModalInterface` takes a `deploy_attempt_timeout_seconds`, defaulting to and bounded above by `DEPLOY_ATTEMPT_TIMEOUT_SECONDS`. Only shortening it is allowed, because `imbue.mngr_modal.routes.deployment` sizes `DEPLOY_MAX_DURATION_SECONDS` from that constant.

## 2026-10-07

Changed: `DirectModalInterface.deploy` makes one attempt and reports a concurrent modification of the same app as `ModalProxyAppLockedError`, instead of retrying it internally for up to five minutes (MIND-337).

Modal locks an app for the duration of a mutation, so concurrent deploys of one app name race and all but one are refused. Retrying the refused deploy here could only ever re-take the lock and publish the same thing again, because this layer does not know what was being deployed. Deciding whether a refused deploy is still needed belongs to the caller that does know, so `mngr_modal.routes.deployment.ensure_function_deployed` now owns the retry; this layer classifies the failure and stops.

- `DEPLOY_ATTEMPT_TIMEOUT_SECONDS` (180s, the `modal deploy` subprocess timeout) is now public, so a caller can size its own retry budget from it.

- `DEPLOY_MAX_DURATION_SECONDS` moved to `imbue.mngr_modal.routes.deployment`, which is now what the bound describes.

Modal reports a sandbox it has ended -- a worker lost on the way up, an idle or lifetime limit reached -- under whichever gRPC status its teardown has reached: `NOT_FOUND` ("Modal Sandbox with container ID ... not found. This means this Sandbox has already shut down") once its control plane has dropped the task, `INVALID_ARGUMENT` ("Modal Sandbox is shutting down") while it is still on its way out. Both reached callers as `ModalProxyNotFoundError` / `ModalProxyInvalidError`, indistinguishable from a resource that genuinely never existed and from a genuinely bad argument.

Added: `ModalProxySandboxGoneError`, raised for either wording, so callers can tell that the sandbox a call was about will not run commands and has to be replaced rather than waited on. It subclasses `ModalProxyNotFoundError`, so teardown paths that already treat an already-gone sandbox as benign keep working unchanged.

Fixed: `ExecProcess.wait()` and `ExecOutput.read()` now translate Modal's exceptions like the rest of the boundary. A command's exit code and its output both stream from the sandbox's task, so Modal ending that task mid-command used to leak a raw `modal.exception.NotFoundError` past callers that only catch `ModalProxy*` errors.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-29

Modal's transient control-plane failures are now recognized as transient. Modal routes four gRPC statuses -- `CANCELLED`, `UNKNOWN`, `DEADLINE_EXCEEDED` and `UNAVAILABLE` -- to a single `modal.exception.ServiceError`, which the error translation had no branch for. All four therefore arrived as the generic `ModalProxyError`, which callers read as "Modal answered, definitively, no": a control-plane call that failed `UNAVAILABLE` was not retried at all, while the same call failing `INTERNAL` was.

`ModalProxyServiceError` now covers that class, and `ModalProxyTransientError` is the single statement of which Modal failures are transient -- `ModalProxyInternalError`, `ModalProxyRateLimitError` and `ModalProxyServiceError` all derive from it, so callers catch the base instead of each listing the concrete types.

Retry decorators now sit *above* the exception translation rather than below it, so `_is_transient_modal_error` and `_transient_wait` decide on `ModalProxy*` types. Both previously kept their own parallel list of the Modal SDK classes those came from, and the post-deploy function lookup retried on `modal.exception.NotFoundError`; all three now read the shared hierarchy, so a new transient subclass is covered without editing any of them.

A contract test reads Modal's own list of retryable statuses rather than restating it, so it fails if Modal adds a status or moves one (MIND-274).

## 2026-09-26

Merged the `mngr/modal-env-not-found-not-transient` branch, which stops treating a Modal "Environment '...' not found" answer as a transient failure: read-path constructions of the Modal provider (`mngr list`, `mngr gc`, the garbage collection after `mngr destroy`) against a profile that has never created its Modal environment are now skipped on the first answer instead of after the 60-second retry budget. The fix is described in `mngr-modal-env-not-found-not-transient.md` in this directory, which lands in the same PR; this branch adds nothing further to modal_proxy.

A Modal answer that the environment does not exist is no longer retried as a transient failure. Every read-path construction of the Modal provider (`mngr list`, `mngr gc`, the garbage collection after `mngr destroy`) against a profile that has never created its Modal environment used to spend the whole 60-second retry budget logging `Modal failed transiently (Environment '...' not found)` before the provider was skipped; it is now skipped on the first answer. The host-create path keeps its own bounded retry for the environment it has just created.

## 2026-09-25

Read-only Modal control-plane calls now wait out a transient Modal failure instead of failing the caller's whole command (MIND-312).

Retry used to be attached to `DirectVolume`'s methods and nowhere else, so whether a blip was survived depended on which object the call hung off rather than on the failure. `sandbox_list`, `Sandbox.get_tags()`, `Sandbox.poll()`, `Sandbox.tunnels()`, `sandbox_from_id`, `volume_list`, `is_function_deployed` and the app lookup -- the reads every discovery makes -- had none at all, so one transient status from Modal ended the operation immediately. They all share one retry policy now, keyed on the error.

The budget is stated in seconds rather than in attempts, and is 60s. Modal's own client gives a failing control-plane connection a 63s `total_timeout`, which is its statement of how long its blips last; the previous `stop_after_attempt(5)` amounted to about 15s, and CI showed Modal `INTERNAL`s outlasting it.

Backoff now carries up to 2s of jitter. Discovery fans dozens of volume reads out at once, so a blip fails them all at the same instant; an undithered curve marched them back in lockstep, which is how waiting out a blip provokes Modal's rate limit. The jitter spreads each wave out rather than reducing the fan-out.

Each retry is now logged at warning level with the error and how much of the budget is spent, so how long Modal's blips actually last is visible rather than guessed at.

Mutations (creating a sandbox, setting tags, taking a filesystem snapshot, deleting a volume) are deliberately not retried: Modal may have applied one before failing to report on it.

## 2026-09-21

Added `ModalInterface.is_function_deployed`, which answers whether an app's current deployment publishes a function of a given name. Unlike looking a function up and reading its web URL, it answers once rather than riding out Modal's post-deploy read-consistency window, so an absent function is reported immediately instead of after a retry budget. `mngr_modal` uses it to decide whether an app already carries a route endpoint.

`ModalInterface.deploy` now takes `extra_env`, added to the environment the deployed script runs under, for scripts that read their configuration from the environment.

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-18

`FakeSandbox` (the test double behind `FakeModalInterface`) runs commands on the machine running the tests. It now refuses any command that reaches for mngr's host bring-up (anything mentioning `/etc/ssh`, `/usr/sbin/sshd`, or `apt-get`) and raises `FakeSandboxRefusedHostProvisioningError` instead of executing it. Before, a test that drove the real Modal `create_host` through the fake would, when the suite ran as root (for example the vendored copy inside a minds workspace container), replace the machine's real sshd host key, overwrite root's `authorized_keys`, and apt-install packages; the workspace's owner was then locked out with an unfixable "workspace unresponsive".

`FakeExecProcess` now reports the real exit code of a completed command instead of always 0, and takes its finished output and exit code as fields, so a sandbox fake can answer a command without executing anything. `FakeModalInterface._build_sandbox` is the hook a subclass overrides to hand out such a sandbox.

## 2026-09-16

Failed image builds can now be asked for their logs.

`ImageInterface.fetch_build_logs()` returns the build output of an image's final layer, fetched from Modal rather than read off the live log stream. Call it on the image that failed to build: Modal resolves it through the failed build attempt it keeps on that object. Modal indexes a failed build's log a few seconds behind the failure and fills it in a piece at a time, so the call waits (up to 15 seconds) for the build to show as terminated before returning.

Modal gives no structural end-of-log signal on that path -- the batches' `eof` and `app_done` flags and the entries' `task_state` all come back unset, and its public log API drops them anyway -- so the wait keys off `BUILD_TERMINATION_MARKER`, the line Modal's builder writes as it terminates a failed task. An acceptance test holds Modal to that line, and giving up waiting logs a warning naming the marker, so a rewording upstream surfaces as a failed test rather than as build logs quietly arriving truncated.

Note that `fetch_build_logs` is abstract, so any implementation of `ImageInterface` outside this package has to grow one. The bundled `FakeImage` gained fields for the two things a build failure can independently produce -- what it streamed, and what Modal will hand over if asked -- so a fake can pose the case where those differ.

`modal.exception.ImageBuildError` now translates to a new `ModalProxyImageBuildError` instead of the general `ModalProxyRemoteError`, so callers can tell a build failure apart from other remote failures and know there are build logs to go and get.

## 2026-09-12

Integration branch for the imbue_cloud slice-fleet generation 2 program (raw qemu slices with routed-tap networking on Debian 13 boxes, replacing lima), merged together with `main` and the three pre-cutover fix PRs: imbue-ai/mngr-internal#857 (SSH certificates from Vault, #850), #855 (DHCP placement, #849) and #856 (the upstream artifact mirror, #851). The per-change details live in this directory's constituent entries, listed below.

`modal deploy` retries on Modal's app-lock error for up to five minutes of elapsed time and exposes `DEPLOY_MAX_DURATION_SECONDS`.

Constituent entries: `mngr-mirror-upstream-artifacts.md`

- `modal deploy` retries on Modal's app-lock ("concurrent modification") error for up to five minutes of elapsed time instead of six attempts (~45s). Contention is a queue: CI fans dozens of creates out against one shared app name, and concurrent CI runs stack on top, so the lock can stay held for minutes while queued deploys drain; the short window lost whole acceptance runs to that storm.

- `imbue.modal_proxy.direct` exposes `DEPLOY_MAX_DURATION_SECONDS`, the longest `deploy()` can block (retry budget plus the final backoff and per-attempt subprocess timeout), so callers that wait on a deploy from another thread can size their deadline from it instead of guessing.

## 2026-09-11

`VolumeInterface` gained `get_object_id()`, which resolves a volume against Modal and returns its id, raising a not-found error when it does not exist. This is the primitive for answering "does this volume exist?" -- previously callers had to list the volume's files, which goes through Modal's separately rate-limited `VolumeListFiles` API and so conflated "Modal is busy" with "the volume is gone" (MIND-244).

## 2026-08-30

Bumped the pinned `modal` SDK from `1.4.3` to `1.5.4` and opted every real Modal Sandbox operation into Modal's V2 Sandbox backend: `DirectModalInterface` now sets `MODAL_SANDBOX_V2=1` on construction (via `setdefault`, so an explicit `MODAL_SANDBOX_V2=0` still opts out). The V2 Sandbox backend gives substantially higher sandbox creation rates and concurrency. Modal resolves the flag live per call, so `Sandbox.create()`/`Sandbox.list()` use V2 with no other code changes, and GPU sandboxes transparently fall back to the V1 backend inside Modal. Also moved the `FileEntryType` import to `modal.types`, its new home in 1.5.4.

Added a `poll()` liveness probe to the sandbox interface (implemented by both the direct and fake backends): it reports a sandbox's authoritative running state, unlike mere presence in `Sandbox.list`, which under V2 can still include a just-terminated sandbox. The direct backend also now terminates sandboxes synchronously (`terminate(wait=True)`): V2's default fire-and-forget terminate leaves the sandbox running (and listed, and polling as alive) for a window afterward, so blocking until it is actually gone -- matching the fake's synchronous terminate -- is what makes the liveness probe reliable immediately after a stop or hard kill.

## 2026-08-21

Added `ModalProxyConnectionError`, raised when the Modal control plane could not be reached at all (a dropped network or a Modal outage). It is translated from the SDK's `modal.exception.ConnectionError`, which previously fell through to the generic `ModalProxyError` and so was indistinguishable from a failure Modal actually answered with. Consumers can now tell "Modal was never reached, so its state is unknown" from "Modal said no" -- the distinction `mngr`'s modal provider uses to decide whether it is temporarily unavailable or genuinely broken.

Added `UnreachableModalInterface` to the testing helpers: a `FakeModalInterface` whose network-crossing calls fail the way an unreachable Modal does, for testing consumers' offline behavior without a network.

## 2026-08-16

Modal deploys now retry on the second wire shape of the concurrent-modification race ("Function fu-<id> not found" when a winning concurrent deploy finalizes a new app version and the losing deploy's freshly-minted function id vanishes mid-flight), instead of failing host creation outright. Cherry-picked from the mngr/download-attribution branch (commit 0f8fb29d41) after this exact race failed the Modal acceptance suite on this PR's CI runs; merges cleanly with that branch whichever lands first.

Fixed a flaky class of Modal acceptance-test failures: `modal deploy` retries already covered the "selected app is locked" concurrent-modification race, but the same race has a second wire shape -- the losing deploy failing with "Function fu-<id> not found" when the winning deploy finalizes a new app version mid-flight. That shape is now classified as the same retryable transient, so concurrent deploys of the shared snapshot function ride through it instead of failing host creation.

Merged the download-attribution feature branch, which carried a fix classifying the "Function fu-<id> not found" wire shape of the Modal concurrent-deploy race as the same retryable transient as the app lock. The fix is described in `mngr-download-attribution.md` in this directory, which lands in the same PR; this branch adds nothing further to modal_proxy.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-29

Volume operations now use a longer retry backoff specifically for Modal rate-limit errors ("VolumeListFiles rate limit exceeded"): 5/10/20/30 seconds between attempts (~65s of total headroom) instead of the ordinary transient curve's 1/2/4/8 seconds. Rate limits are shared across every concurrent client (e.g. CI's parallel acceptance sandboxes), so a burst regularly outlasted the short curve and surfaced as flaky volume-test failures. Other transient errors keep the original fast curve so genuine failures still surface quickly.

Combined-fixes integration PR (carries mngr/fix-remote-workspace): volume operations use a longer retry backoff specifically for Modal rate-limit errors (5/10/20/30s between attempts, ~65s total headroom) while other transient errors keep the fast 1/2/4/8s curve. See `mngr-fix-remote-workspace.md` in this directory for details.

## 2026-07-07

`ModalInterface.sandbox_create` now accepts an optional `experimental_options` mapping, mirroring `modal.Sandbox.create`. This lets callers request Modal's VM runtime (https://modal.com/docs/guide/vm-sandboxes) via `experimental_options={"vm_runtime": True}`.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

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
