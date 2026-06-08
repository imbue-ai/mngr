# Unabridged Changelog - mngr_pi_coding

Full, unedited changelog entries for the `mngr_pi_coding` project, consolidated nightly from individual files in `libs/mngr_pi_coding/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-04

Fixed remote provisioning of pi resource directories (skills/prompts/extensions/themes) to transfer with a single rsync (`host.copy_local_directory`) instead of uploading each file individually over SSH. The per-file approach opened an SFTP channel per file (a full round-trip over the tunnel) and did not scale to large resource sets -- the same failure mode as github issue 1825.

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

Update pi-coding plugin to use the structured `TmuxWindowTarget` type for tmux
pane targeting. `_send_enter_and_validate` now takes
`tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
