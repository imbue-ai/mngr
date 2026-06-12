# Unabridged Changelog - mngr_vultr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_vultr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-10

Raised the stale coverage floor from 60% to 70% to match the coverage CI already measures (~72%).

## 2026-06-08

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-29

User-visible: minds workspaces running on Vultr (docker-on-VPS) hosts can now
be backed up off-site (restic) when a backup provider is selected at creation
time.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)

Vultr hosts created by `mngr create --provider vultr` now back their per-host
unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem
on the VPS (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`). This
makes future consistent snapshotting of the agent data via
`btrfs subvolume snapshot -r` possible. See `mngr_vps_docker`'s changelog for
the full mechanism.

**Breaking change:** existing vultr hosts created before this release cannot
be discovered or managed after upgrade. Destroy and recreate them.

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

`mngr_vultr` now only contributes the tag-listing; the shared parallel-SSH discovery has been lifted into the `VpsDockerProvider` base class behind a new `_list_provider_vps_hostnames()` seam method.
