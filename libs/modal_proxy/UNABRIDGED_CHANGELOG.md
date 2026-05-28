# Unabridged Changelog - modal_proxy

Full, unedited changelog entries consolidated nightly from individual files in `libs/modal_proxy/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
