# Unabridged Changelog - imbue_common

Full, unedited changelog entries consolidated nightly from individual files in `libs/imbue_common/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-23

- Registered a repo-wide `smoke` pytest marker in the shared markers list, so
  any project can mark a test that runs after a deploy or a release against the
  published artifact and the deployed production environment. See
  `style_guide.md` ("Types of tests") for the category.

## 2026-10-20

The `while True` and inline-import ratchet checks take `excluded_patterns`, as the bare-print check already did, so a project can exempt modules that carry a program as a string to run elsewhere.

## 2026-10-19

The inline-imports ratchet (`PREVENT_INLINE_IMPORTS`) now requires a bare `import` to be the whole statement (modules, aliases, then at most a trailing comment or a `;`-separated statement such as `import pdb; pdb.set_trace()`), so an indented prose line in a docstring that happens to start with the word "import" no longer counts as a violation. `from ... import` matching is unchanged.

## 2026-10-16

The bare-tmux-target ratchet accepts targets that are tmux ids: `$N` / `@N` / `%N` literals,
`$TMUX_PANE`, and shell variables whose name ends in `_ID`. tmux ids are unique for the object's
life and are never prefix-matched, so they need no `=` exact-match prefix.

## 2026-10-14

The repo-wide type-check gate (`test_no_type_errors`, backed by `check_no_type_errors`) now
reports what actually failed. It runs `ty check --output-format concise`, which prints one
line per diagnostic with a fixed grammar, parses that grammar into a `TypeCheckOutcome`, and
builds its failure message from the parsed facts.

Before, the gate counted errors by scanning ty's human-formatted output for lines starting
with `error[` or containing `error:`, then dumped the whole output into the assertion. Both
halves misreported. The substring search also matched source lines that ty echoes for
context, so a run whose real content was three `unknown-argument` errors was announced as
"6 error(s)" -- the extra three were one echoed parameter declaration, `transition_error: str
| None = None`, quoted once per error. And because ty sorts diagnostics by path, the dumped
output opened with whichever diagnostic sorted first, which in that run was a
`possibly-missing-submodule` *warning* from `apps/`. ty exits zero on a warning-only run, so
that warning could not have failed anything -- but it was the text that landed in the JUnit
`message` attribute and the CI check-run summary, and from there it was read as the cause and
filed as a flaky test (MIND-319) against a branch that simply had three type errors.

The message now opens with the exact error count and lists the error-severity diagnostics
first. Warnings follow under a heading that says ty exits zero on them, so they did not cause
the failure. Stdout that matches neither the diagnostic grammar nor ty's own summary line is
reported as unparsed rather than dropped, so a ty crash or an output-format change surfaces
instead of quietly shrinking the counts, and a non-zero exit with no diagnostics still fails
with its own headline. One `ty check` run per test, as before.

What fails the gate is unchanged: error-severity diagnostics, exactly as ty decides. Warnings
are shown but still do not fail CI. The switch to the concise format drops each diagnostic's
source snippet from the message; the message closes by naming `uv run ty check` as the way to
get it back. In exchange the whole diagnostic list now fits inside the 2000 characters CI
shows -- ten diagnostics came to 12.8 KB in the default format and 1.8 KB in this one.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

The source of those groups, `standard_ratchet_checks.py`, carries the same markers in place of its banners; the sync script derives the group order from it, so adding a group needs no script edit.

## 2026-10-08

The `Args:`-in-docstrings ratchet no longer counts a name that ends in `Args` followed by a colon, such as the return annotation `-> ShowArgs:` or `class FooArgs:` a few lines after a docstring. A real `Args:` section still counts, so a project's count can only go down.

## 2026-10-05

The trailing-comments ratchet no longer counts a hex colour that closes a string literal (`assert "background: #e9ecd9" in body`) as a comment. Its hex-colour exemption accepted a colour followed by `;` or whitespace only; a closing quote now counts as well.

Unit tests for the shared regex rules live in `ratchet_testing/common_ratchets_test.py`. Their fixtures hold the anti-patterns the rules look for, so that file joins `test_ratchets.py` and `standard_ratchet_checks.py` in the files every regex ratchet skips.

Added the `raw ConcurrencyGroup.start_new_thread calls` common ratchet (`PREVENT_RAW_START_NEW_THREAD`, `check_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, which destroys the thread's gevent Hub when its target returns. Test files and the helper's own module (`thread_cleanup.py`) are excluded.

## 2026-10-02

- The shared `pytest_sessionfinish` hook now runs the resource_guards session-cleanup callbacks (see `register_session_cleanup_callback`) before stopping the guards, so a library's leak sweep runs in every session its guards are active in. The guards are stopped even when a callback ends the session (for example a sweep failing the run with `pytest.exit`), and the per-test durations are printed before the callbacks run so they stay visible in that case too.

## 2026-09-30

Ratchet failure messages now say what to do when a count legitimately has to rise: change only the number, and put the reason in your response and the PR description rather than in a comment. Ratchet files record counts, not history, and the comments explaining past count bumps went stale as soon as the next bump landed.

The 9 such comment lines that had accumulated in `libs/imbue_common/imbue/imbue_common/test_ratchets.py` have been removed.

## 2026-09-29

The JSONL log records written through `imbue_common` logging now include the formatted traceback of a logged exception, in a new `exception.traceback_text` field, capped at 8,000 characters (keeping the innermost frames). Previously they recorded only the exception's type and message plus a `traceback` boolean, so a log file alone could not show where an unexpected exception was raised. The `traceback` boolean is unchanged.

`format_exception_traceback`, the capped formatter this uses, moved here from mngr (`imbue.mngr.utils.error_utils`) as `imbue.imbue_common.tracebacks`.

## 2026-09-25

Release tests now get a 300 s per-test budget at collection, the way `tmux` tests already get 60 s. A release test drives real `mngr` subprocesses, each paying a 10-20 s CLI cold start, so the repo-wide `--timeout=10` never fit; the e2e suite had grown 291 hand-written `@pytest.mark.timeout` markers to escape it, and every new test had to remember one. The budget is sized from the 2026-09-21 TMR run's measured durations (140 e2e tests: median 29 s, slowest 194 s), applies only when the command-line `--timeout` is lower, and yields to a test's own marker, so a test that genuinely needs more still declares one. `RELEASE_TEST_TIMEOUT_SECONDS` is exported so callers that pin a matching flag (TMR) can stay in lockstep.

## 2026-09-24

Sentry events are no longer discarded when a connection accepts an envelope and then never answers.

Before, the SDK's transport was built without any retry configuration, so urllib3's default applied. That default retries connection failures for any method, but gates read failures on the method being idempotent -- and envelopes are POSTs. So a send that never got a connection was already retried, while one written into a connection that then went silent was dropped for good -- recorded as a discarded event, but never reported back to whoever had sent it. In production this was costing more events than were being delivered: over three days the client discarded 550 events as `network_error` against 505 accepted.

urllib3 detects and replaces a pooled connection the peer closed, measured for a clean close and for a reset, so neither of those loses anything; what was left unretried was the send that got no answer at all. That fits a client which is long-lived, sends rarely, and moves between networks, though which connection state produced any particular production loss was not established.

Now those sends are retried, while a rate-limit or size-limit response still goes straight to the transport's own handling rather than being retried. A user-submitted bug report is the case this matters most for: it is reported to the user with an event id at the moment it is queued, so a send that failed was indistinguishable from one that filed. Retries narrow that window; they do not close it, and reporting delivery rather than queueing remains a separate change.

## 2026-09-23

The direct-subprocess ratchet no longer counts a subscripted type annotation such as `subprocess.Popen[bytes]` as a violation, since an annotation spawns nothing. Calls, references and imports of `subprocess.Popen`, `subprocess.run` and the rest still count, so a project's count can only go down.

## 2026-09-21

Added the `raw ConcurrencyGroupExecutor construction` common ratchet (`PREVENT_RAW_CONCURRENCY_GROUP_EXECUTOR`, `check_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, whose workers destroy their gevent Hub on exit. Test files and the `mngr_executor` chokepoint (`thread_cleanup.py`) are excluded.

## 2026-09-17

The rationale of the `find_per_file_host_uploads_in_loops` ratchet no longer describes a per-file `write_file` call as an SFTP channel open: `write_file` now runs on a single SSH exec channel, so the docstring names the SSH channel open each call still costs. No behavior change.

## 2026-09-16

`imbue-common` gains a `testing` extra -- `import-linter`, `inline-snapshot`, `pytest` -- declaring
what a downstream test suite needs to use the test library the wheel ships
(`imbue.imbue_common.ratchet_testing`, and now `imbue.imbue_common.pytest_utils`, which was
excluded from the wheel). `ratchet_testing` imported `import-linter` with no dependency naming it,
so it only worked for consumers that happened to install `import-linter` themselves. This repo's
own pytest conventions (`conftest_hooks`) stay out of the wheel, so a plain install still pulls in
no pytest. `build_test.py` installs the built wheel with `[testing]` into a scratch venv and imports
the test library.

## 2026-09-13

Dropped the section comments in `conftest_hooks_test.py` that labelled which block of tests
covered which helper (`# _read_lock_info / _write_lock_info` and eight others). Every test in
the file already names its subject -- `test_read_lock_info_invalid_json`,
`test_break_stale_lock_expired_deadline_kills` -- so the labels restated the names directly
below them and were one rename away from being wrong.

Test-only, and no test was added, removed, or renamed: the same 36 tests run.

Tests that drive a real external resource now inherit a budget sized for that class instead
of the repo-wide 10s `--timeout`. The shared conftest hooks carry a per-marker table of test
budgets, and any collected test carrying one of those markers gets it applied at collection
time. Today the table holds one entry: `tmux` tests get 60 seconds.

A test that declares its own `@pytest.mark.timeout` is left alone -- that decorator is how a
test says it differs from its class, and it still wins. The class budget is also declined
when the run's global `--timeout` is already at least as generous, so `pytest --timeout=300`
while debugging is not silently narrowed back down.

The 10s default is sized for the style guide's "each individual unit test runs quickly
(< 5 seconds)". Tests that create a real tmux session through the real CLI are integration
tests doing ~28 sequential subprocess spawns; measured across the 136 such tests that had no
timeout of their own, they cost a median of 1.78s and at most 3.60s on idle hardware. Against
10s that absorbs only a 2.8x slowdown, which CI's parallel contention regularly exceeded --
so the timeout was firing as a false positive about the harness rather than reporting anything
about the product.

## 2026-09-11

Added the `UnitFloat` primitive: a float constrained to the closed unit interval `[0.0, 1.0]`, validated on construction and through a pydantic schema.

It mirrors the existing `Probability` but without claiming to be one, for fractions and ratios that are not chances -- a required completion fraction, a pass rate, a proportion of a budget.

## 2026-09-09

Added `imbue.imbue_common.suspension`, which answers whether the machine was suspended between an earlier moment and now by comparing how far the wall clock outran the monotonic one (which stops during a suspension). Used by mngr and the `mngr forward` plugin to retire SSH connections that outlived a sleep, since such a connection is dead whatever the socket claims.

The standard ratchet checks `check_async_await` and `check_asyncio_import` now take an optional `excluded_patterns` tuple, matching `check_getattr` and the other checks that already accept one. A project can exempt files whose async surface is dictated by a third-party interface, so its recorded count tracks only the async it chooses. Existing call sites are unaffected: the parameter defaults to `()`.

## 2026-09-08

- Ratchet scans skip binary files instead of dying on them. A scan with no extension filter reaches
  every non-ignored file in a project, so a project that vendors a frontend -- tracking fonts and
  icons beside its source -- would fail the whole ratchet suite with a `UnicodeDecodeError` rather
  than report its violations. A file that is not text cannot contain a text pattern, so excluding
  it costs no coverage.

- Ratchet scans exclude binary files in two passes, and read undecodable bytes as replacement
  characters instead of raising. `BINARY_FILE_EXCLUSION` (now covering font formats too) is applied
  centrally before any file is opened, so every scan gets it rather than only the two that
  remembered to pass it; a NUL-byte sniff of what remains catches the formats the list does not
  name, which is what a vendored frontend's assets would otherwise have taken the scan down with.

## 2026-09-01

Added an optional `excluded_patterns` parameter to the shared `assert_ratchet` helper and six standard ratchet check wrappers, so projects can carve vendored third-party code out of ratchet scans.

Added an optional `excluded_patterns` parameter to the shared `assert_ratchet` helper and to six standard ratchet check wrappers (`check_builtin_exception_raises`, `check_getattr`, `check_args_in_docstrings`, `check_returns_in_docstrings`, `check_literal_with_multiple_options`, `check_if_elif_without_else`), matching the existing `check_bare_print`/`check_direct_subprocess` precedent, so projects can carve vendored third-party code out of ratchet scans.

## 2026-08-29

`imbue_common.event_envelope` now exports `parse_iso_timestamp`, which reads an `IsoTimestamp` (nanosecond-precision ISO 8601 with a `Z` suffix, offsets also accepted) into an aware UTC `datetime`, or `None` when it will not parse. The minds hub's restic and git readers use it instead of each carrying their own sub-microsecond trimming.

The `if/elif without else` ratchet no longer flags an `else:` block whose body happens to begin with an `if`. Such a block is a real else clause, not a continuation of the chain -- an `elif` parses into an `orelse` holding the `if` and nothing else -- so the ratchet now only follows the chain when the `orelse` holds a single `If`.

This is the merge of `gabriel/tactful-swift` (the inner workspace-updates work) into current `main`. It carries no changes of its own beyond resolving the merge; see that branch's `gabriel-tactful-swift.md` entry for what actually changed.

## 2026-08-26

No behaviour change. Pinned, with a test, that a log attachment group capping its file count sweeps the newest matching file by modification time. Bug reports rely on this for rotated logs: the groups that carry them keep exactly one file, and keeping any other rotation would still produce a well-formed report -- one holding the bytes that do not cover the incident.

## 2026-08-24

Error reports now log each file they select as an attachment, with its size, before any upload is attempted.

Previously the only record of what a report attached was the S3 URIs on the event itself. In an environment with no bucket configured those URIs are all null, which reads the same as a group that matched no files — so there was no way to tell "nothing was collected" from "collected, but this environment does not upload". The selected files are now named in the logs either way.

Manual bug reports can now attach files one-shot, by exact path, instead of through the process-global attachment groups. Groups are swept onto every event, which is right for rolling logs and wrong for files a user consented to attach to one specific report — anything a group could match would ride along on every unrelated error event until it was deleted. One-shot report files appear on exactly the event they were staged for and on nothing else.

A report's attachment URIs can now be reserved before the files exist. S3 keys are minted locally, so a bug report can publish where its attachments will be readable, be sent immediately, and have the bytes uploaded to those exact keys once collection finishes — the submitter no longer waits on collection to get an event id. A reserved attachment that never materializes (dropped for secrets, or a failed collection) is logged and leaves its object absent rather than failing the rest of the report's uploads.

An attachment that already carries its own compression is now uploaded as it is, under a key that says so. A `.zip` keeps the `.zip` suffix and is stored verbatim; everything else is still gzipped and keyed `.gz`, exactly as before. Gzipping an archive buys nothing and would leave whoever follows the URI unwrapping two layers to reach the file they asked for. Whether the bytes were compressed is decided the same way on both upload paths — from the file's own name when it is uploaded in one shot, and from the reserved key's suffix when the bytes arrive later — so the object at a URI can never contradict what that URI already told a reader to expect.

Manual bug report events no longer lose the S3 URIs of their own attachments. The sentry SDK's client-side serializer trims the event's `extra` dict (and every dict/list nested under it) to its first 10 entries in insertion order, and a full-size report carries 15 extra keys -- so the reserved `uploaded_files_bug_report_workspace` / `_console` / `_attachment_status` pointers, inserted last, were silently dropped from the stored event even though the files themselves uploaded fine.

`setup_sentry` now raises the SDK's databag breadth cap (`raise_sentry_databag_breadth`, pinned by a test against the real serializer) so no extras are trimmed on any event -- this also restores the `platform` extra and the last swept log groups that automatic error events had been losing to the same cap.

`submit_manual_bug_report` additionally inserts the report's own attachment pointers (description, staged report files, reserved URIs) before the swept log-group extras, so if truncation ever returns, a swept log group falls off instead of the pointers to the files the user consented to attach.

## 2026-08-22

`IMAGE_PINNED_PACKAGE_NAMES` gains the new `analytics` app, so `just export-image-requirements`, the per-app drift tests, and the deploy preflight cover its hash-locked image export.

## 2026-08-21

Carries the workspace-wide sentry-sdk version bump (past the FastAPI sync-endpoint wrapper leak) via a merge of `mngr/env-tier-sentry`, ahead of that branch landing on main.

## 2026-08-20

The `sentry-sdk` floor moves to 2.63.0 (the version that fixes the per-request FastAPI sync-endpoint wrapper leak that took down the staging connector, mngr-internal#493).

## 2026-08-19

Doc-only: comments/docstrings updated for the operator-CLI consolidation -- `minds env deploy` is now `minds-admin env deploy` and `mngr imbue_cloud admin server await-delivery` is now `minds-admin server await-delivery`; `modal_image_requirements.py`'s module docstring now states the current rationale for living in the public `imbue_common` (its remaining consumers moved to the private minds_admin app, but the mirror's `imbue_common` must stay self-contained).

## 2026-08-16

`EventEnvelope` now ignores unknown fields (previously it inherited `FrozenModel`'s `extra="forbid"`). Persisted event records are cross-process, cross-version wire data: a shared append-only events.jsonl is routinely read back by an older program version than the one that wrote it, and strict parsing meant one additive field permanently wedged every older reader (mngr-internal#422). Schema evolution on event models must be additive-with-defaults; a repo-wide meta check now bans any `EventEnvelope` subclass from re-tightening `extra` to `"forbid"`.

- Added `owner_exec_client.py`: a signed client for the owner-exec service
  (SSH-equivalent authority over a workspace over HTTP). It signs requests per
  the RFC 9421/9530 strict profile with an Ed25519 key and verifies every
  response (and the `/run` stream trailer) against the endpoint's pinned SSH
  host key, failing closed on a bad signature. Cross-checked against the shared
  vectors published by the `imbue-ai/owner-exec` repo.

- The shared `PREVENT_EXEC` ratchet regex no longer misfires on hyphenated
  prose like `owner-exec (vm role)` inside string literals.

## 2026-08-12

Merge the web-only work (`mngr/hopefully-last-web-details`) into main. Registered the new `oauth-redirector` app in `IMAGE_PINNED_PACKAGE_NAMES` so its Modal image requirements are exported and drift-checked like the other pinned Modal apps.

Register the new `oauth-redirector` app in `IMAGE_PINNED_PACKAGE_NAMES` so its Modal image requirements are exported and drift-checked like the other pinned Modal apps.

## 2026-08-04

Fixed the `trailing comments` ratchet firing on `#NNNN` issue and PR references that are not comments at all. It already exempted `PR #NNNN` in prose, but a bare `#NNNN` -- in prose or inside a string literal, such as a test asserting that a cell renders `#2482, #2392` -- still counted as a trailing comment.

A `#` immediately followed by a run of digits that ends there is now exempt everywhere, which leaves a trailing comment like `#2nd attempt` still counting. Sixteen lines across the repo lose their match and every one of them is a false positive: two in `mngr_codex`, thirteen in `mngr_kanpan`, and one under `scripts/`, which no `test_ratchets.py` covers. Only `mngr_codex`'s recorded count moves, because the thirteen in `mngr_kanpan` are lines this branch adds -- no recorded count ever counted them.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the `PREVENT_UNPINNED_MODAL_PIP_INSTALL` common ratchet rule and its `check_unpinned_modal_pip_install` wrapper: Modal images must install from a committed hash-locked requirements export (see `libs/modal_app_kit/README.md`) instead of passing bare package names to `pip_install`/`uv_pip_install`, which resolve at image-build time.

Added `modal_image_requirements.py`: the pure side of the pinned Modal image exports (the canonical offline `uv export` command, the pinned-app registry, and export paths). It lives here rather than in `modal_app_kit` so the public mirror's `minds env deploy` freshness preflight can use it (modal_app_kit is not part of the public tree).

## 2026-08-01

The shared async/await ratchet no longer counts prose as async usage.

The rule matched the bare keyword on a word boundary, so it also fired on `await-delivery` -- a real CLI subcommand (`mngr imbue_cloud admin server await-delivery`) that naturally appears in comments, docstrings, and user-facing strings. Most recorded matches across the repo were such false positives, which meant every comment naming that command silently cost a ratchet slot and could fail CI for a project that had no async code at all.

The rule now matches what actually follows the keyword in Python -- whitespace before the awaited expression, or an opening paren -- so both real spellings are still caught while a trailing hyphen is not. Recorded counts were re-snapshotted accordingly: `mngr_imbue_cloud` 7 to 0 and `mngr_pi_coding` 8 to 7, which is exactly the prose that stopped being counted; `mngr_gcp` 1 to 0, a slot that was unused under either pattern; and `imbue_common` 3 to 2, whose remaining matches are this rule naming itself in its own source. No project's count dropped because real async code stopped being counted.

## 2026-07-31

Registered a new shared pytest marker, `witnesses(coordinate, partial=None)`, in the centralized marker list.
Tests anywhere in the monorepo can now declare which behavior unit they verify (by its coordinate, e.g. `browser-authorization.fresh-code`), with `partial=` noting what the test does not cover.
See the `behaviors` skill for the coordinate system.

A user-submitted bug report's description is no longer lost when Sentry's data scrubber redacts it.

Sentry's default data scrubber replaces an entire string value with `[Filtered]` whenever the value matches its built-in password pattern anywhere -- and that pattern is a list of plain substrings (`auth`, `secret`, `password`, `credentials`, ...), so ordinary prose trips it. Roughly one in nine reports was arriving with no body at all; in one case the word "authored" destroyed a 9,705-character report.

`submit_manual_bug_report` now takes the description as an explicit argument (rather than reading it back out of the caller's report mapping, where a rename in the caller would silently disable the fix) and uploads it verbatim to the same S3 bucket already used for log attachments, recording its `s3://` URI on the event under `uploaded_files_bug_report_description`. The inline copy is still sent -- it survives for most reports and is the convenient one to read -- but it is no longer the only copy. Reports with no log attachments get the out-of-band copy too.

The description travels the same way every other bug report attachment does, so there is one carrier to reason about rather than two. It inherits that carrier's properties: no copy is made in environments with no bucket configured (minds' `development`), and the URI is recorded whether or not the upload itself succeeds.

Each manually-submitted bug report now also gets its own Sentry issue, via an explicit random fingerprint. Sentry's default grouping is driven by the stack trace, which is the submitting code path and therefore identical for every report, so all reports were collapsing into a single catch-all issue whose title only ever showed the newest one -- and assigning, resolving or commenting on one report meant doing it to all of them.

Submitting an error report no longer risks crashing the whole process.

The Sentry S3 attachment uploader used to share one `boto3` client across its upload thread pool. A boto3 client owns one urllib3 connection pool and therefore one `SSLContext`, and urllib3 calls `load_verify_locations()` on that context for every new connection it opens -- mutating the context's OpenSSL `X509_STORE` while other threads read the same store, unlocked, to verify the server's certificate. A report that uploads several logs at once opens all of its connections at the same moment, so the uploads raced and could segfault the process (in minds this killed the backend and surfaced as "Minds stopped unexpectedly" right after submitting a report).

Each upload thread now creates and reuses its own S3 client, giving every TLS handshake its own certificate store with no concurrent writer. Uploads still run in parallel; the client is created lazily on each thread's first upload rather than eagerly at Sentry setup.

Those clients are built from a session the uploader owns, instead of the process-global default session that plain `boto3.client()` resolves. Other modules build clients off that global session without locking, so the uploader's lock could never have made it safe; owning the session makes the lock cover the whole hazard.

The upload pool is also pinned to a fixed 8 workers rather than the thread-pool default of `min(32, cpu_count + 4)`. A report's attachment sweep is on the order of ten files, so eight still clears one in a round or two, and the number of live clients (each about 1MB once it has connected) no longer varies with the machine.

## 2026-07-24

The shared Sentry setup now uses a single reporting gate instead of two.

`setup_sentry` no longer takes an `is_log_inclusion_enabled` callable: the same `is_error_reporting_enabled` callable now gates both automatic error sends and whether their log/traceback attachments are collected. `submit_manual_bug_report` likewise drops its `include_logs` argument and always attaches recent logs when a logs folder is given (still a no-op when no S3 bucket is configured).

## 2026-07-23

Remove two stale organization-specific references from comments in `imbue_common.sentry.core` (an old Sentry stats URL and a dead GitLab merge-request link); no behavior change.

## 2026-07-21

Fixed a false positive in the `trailing comments` ratchet (`PREVENT_TRAILING_COMMENTS`): `#{...}` interpolation/format tokens (e.g. tmux format strings like `"#{client_width} #{client_height}"`) are no longer counted as trailing comments. The `#` in these tokens is interpolation syntax inside a string, not a comment. Real trailing comments, hex colors, and `PR #NNNN` references are unaffected.

## 2026-07-15

`LogAttachmentGroup` gained an optional `base_dir` field: a group with `base_dir` set sweeps that directory instead of the process's log folder, so error reports can attach log files that live outside it (e.g. minds attaching the detached `mngr latchkey forward` daemon's logs). A missing directory simply matches nothing.

Sentry error reports now carry a stable, randomly-generated anonymous user id (no PII) so Sentry can report the number of distinct installs affected by each issue. Added `get_or_create_anonymous_user_id` (persists a random id to a file, race-safe across processes) and a required `user_id` argument to `setup_sentry`, which is attached to every event via `sentry_sdk.set_user`. `send_default_pii` stays disabled; only this opaque id is sent.

Added `secret_wrapping`: pure envelope-encryption helpers (argon2id key derivation, AES-256-GCM DEK wrap/unwrap and secrets encrypt/decrypt) used by the minds workspace-sync feature. New dependencies: `argon2-cffi`, `cryptography`.

## 2026-07-14

Fixed the inline-function ratchet (`find_inline_functions`) double-counting a function nested two or more levels deep. It walked every `FunctionDef` in a file, including nested ones, and descended into all of each one's descendants, so a function with two enclosing functions was emitted once per ancestor. The recorded count therefore overstated the real number of inline functions. Nested defs are now collected keyed by source position and counted once. Only projects that actually nest functions two deep are affected; across the whole monorepo the sole recorded count that changes is `apps/minds` (from 9 to 7 before any code change).

## 2026-07-13

The trailing-comments ratchet (PREVENT_TRAILING_COMMENTS) no longer misfires on `PR #NNNN` references inside comment or docstring prose. The unanchored pattern treated the `#` of a PR number as a trailing comment (even on comment-only lines, since a match can start mid-line); a negative lookbehind now exempts a `#` immediately preceded by `PR `, alongside the existing hex-color and `ty: ignore` exemptions.

`setup_sentry` now accepts an `ignored_loggers` argument: glob patterns for stdlib logger names whose records must never become Sentry events or breadcrumbs. This is needed because Sentry's default logging integration patches `logging.Logger.callHandlers` at the class level, so it captures a logger's ERROR records as events even when that logger has `propagate=False`. Callers that already route a noisy third-party logger's output elsewhere (e.g. into loguru) can now pass those logger names so Sentry drops the raw records instead of flooding on already-handled noise.

## 2026-07-09

- Added: `LowerCaseStrEnum` in `imbue.imbue_common.enums` -- the lowercase sibling of `UpperCaseStrEnum`, for enums whose values are an externally visible, already-lowercase wire format (first used by the pool bake / destroy outcome statuses in `mngr_imbue_cloud`).

## 2026-07-06

Added a shared Sentry error-reporting library under `imbue.imbue_common.sentry`, so multiple Imbue Python processes can report errors without duplicating the machinery. It packages the generic pieces that previously lived in the minds backend: the loguru-to-Sentry event/breadcrumb handlers, the unsigned-S3 attachment uploader, the per-exception rate limiter, the oversized-event (HTTP 413) transport, the `before_send` chain (including the automatic-reporting consent gate and interrupt/clean-shutdown filtering), manual bug-report submission, and a parameterized `setup_sentry`.

The library is intentionally agnostic about *which* Sentry project/environment/bucket a process uses: `setup_sentry` takes a concrete `dsn`, `environment_name`, and optional `s3_attachment_bucket` (plus a `service_name`, the Sentry integrations, and a set of `LogAttachmentGroup`s describing which log files to attach). Project-specific config (the DSNs, the deploy-environment model, and the environment-to-bucket mapping) lives with each consuming project, not here. This pulls in `sentry-sdk`, `boto3`, and `traceback-with-variables` as dependencies of `imbue-common`.

## 2026-07-01

Added the shared `PREVENT_ASYNC_AWAIT` ratchet rule (in `common_ratchets.py`) and the `check_async_await` wrapper (in `standard_ratchet_checks.py`). This powers a new per-project `test_prevent_async_await` ratchet across the whole monorepo that freezes `async def` / `await` usage and prevents new async code from being added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit.

## 2026-06-26

`find_bash_scripts_without_strict_mode` (the helper behind the repo-wide bash strict-mode ratchet) now skips `*.sh` files under `.minds/template/`. Those are declarative secret-schema templates -- commented `export KEY=` files sourced by the deploy tooling (`scripts/push_vault_from_file.py`, `minds env deploy`) and copied per-tier, never executed standalone -- so `set -euo pipefail` is meaningless for them and they are not the class of runnable script the ratchet guards. This also removes a local-vs-CI count skew, since those templates were already absent from the offload build context.

## 2026-06-24

Updated the `PREVENT_HARDCODED_CLAUDE_DIR` ratchet's guidance text to reference
the renamed `find_user_config_in_isolated_mode()` accessor (was
`find_user_claude_config()`). No behavior change.

## 2026-06-19

Clarified the README's one-line description of the library's purpose.

## 2026-06-11

Fixed a bug in the `PREVENT_BUILTIN_EXCEPTION_RAISES` ratchet regex: a trailing `\b` after the opening paren meant it only matched raises whose first argument started with a word character (e.g. `raise OSError(msg)`), missing the common `raise ValueError("literal")` and `raise OSError()` forms. The ratchet now also excludes test files (consistent with tests legitimately raising built-in exceptions to simulate error conditions). Replaced the direct `ValueError` raises in the constrained `primitives` types and the `RegexPattern` validator with dedicated `InvalidPrimitiveValueError` / `InvalidRegexPatternError` exception types.

## 2026-06-10

Raised the stale coverage floor from 88% to 90% to match the coverage CI already measures (~95%), and removed the now-obsolete comment about per-package offload coverage drift (the offload bug that caused that drift has since been fixed, so coverage is deterministic).

## 2026-06-04

Ratchet file scans no longer crash on a tracked symlink that resolves to a directory. The file walker (`_get_all_files_with_extension`) now filters on `is_file()` instead of `exists()`, so a symlink-to-directory (which git lists as a blob but cannot be read as a file) is skipped instead of raising `FileReadError`.

- Refresh the stale test-type docstring in `conftest_hooks.py` that described acceptance tests as running "on all branches except release" and release tests as running "only on release". There is no `release` branch; acceptance tests run on every PR and release tests run via the dedicated Release Tests workflow (manual dispatch and `v*` tag pushes) and TMR. No behavior change.

Added a new common ratchet to the `ratchet_testing` framework: `check_per_file_host_upload` (AST-based `find_per_file_host_uploads_in_loops`) flags `write_file`/`write_text_file`/`put_file` calls inside `for`/`while` loops, steering bulk transfers toward a single rsync (`host.copy_directory`). Recurring per-file-over-SSH uploads have repeatedly caused upload timeouts and 'connection reset / SSH protocol banner' failures (see github issue 1825).

## 2026-06-02

A logging test that imported `BaseMngrError` from `imbue.mngr` (now removed) no longer reaches
into the `mngr` package: it uses a local test-only exception instead. No runtime behavior change.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

Also removed the now-unused `check_no_ruff_errors` helper from
`imbue/imbue_common/ratchet_testing/ratchets.py`: its only callers were the
deleted per-project `test_no_ruff_errors` tests, and the repo-wide ruff test
runs its own `ruff check` / `ruff format --check` invocations rather than using
the helper. (`check_no_type_errors` is kept, since the repo-wide type test uses it.)

No user-facing behavior change.

## 2026-05-27

# ty 0.0.39 suppression syntax

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]`, as required by `ty` 0.0.39 (which no longer honors the mypy-style bracketed form). Affected: the `field_ref` proxy returns in `frozen_model`/`mutable_model`, the `entry_points` cache monkeypatch in `conftest_hooks`, and an event-level assignment in the event-envelope test.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Add a `PREVENT_BARE_TMUX_TARGETS` ratchet rule (and `check_bare_tmux_targets` helper)
that flags `tmux <subcmd> ... -t '<target>'` or `... -t "<target>"` where the quoted
target doesn't begin with `=`. Scans every tracked file type, not just `.py`, so
shell scripts and other non-Python tmux call sites are also covered. Use it from
project ratchet suites (mngr does, via `rc.check_bare_tmux_targets`).

Context: bare-name tmux targets fall back to session prefix matching, which can route
commands meant for a stopped session to a still-running sibling whose name starts with
the same prefix. Routing all `-t` argument construction through the
`TmuxSessionTarget` / `TmuxWindowTarget` classes in `imbue.mngr.hosts.tmux`
(via `.as_shell_arg()`) prepends `=` for exact-match resolution; this ratchet enforces
that convention.

Promote `BINARY_FILE_EXCLUSION` (a tuple of binary-file globs that would otherwise
trip `.read_text()` with `UnicodeDecodeError`) to a public `Final` constant in
`imbue.imbue_common.ratchet_testing.core` so the project ratchets and the repo-wide
meta-ratchets share one canonical list.

## 2026-05-22

- The shared conftest hooks now set `LATCHKEY_DISABLE_COUNTING=1` in `os.environ` once per pytest session. Any subprocess spawned by a test (directly or transitively, e.g. the Latchkey Gateway started by the minds Electron e2e test) inherits the opt-out, so test runs no longer count toward Latchkey's public daily usage counter.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-08

- imbue_common: extend `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` -- aligning with the wheel-exclude pattern from #1505 so `testing.py` and `conftest.py` are uniformly recognized as test code across ratchets. Existing snapshots are not affected (the change can only reduce violation counts; current snapshots are upper bounds).
