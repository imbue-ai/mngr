# Unabridged Changelog - modal_litellm

Full, unedited changelog entries consolidated nightly from individual files in `apps/modal_litellm/changelog/`.

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

- ``modal_litellm``'s README + module docstring drop the wrong
  ``/anthropic`` suffix from the documented ``ANTHROPIC_BASE_URL`` --
  the Anthropic SDK appends ``/v1/messages`` itself, which lands on
  LiteLLM's native route that already accepts the Anthropic request
  shape. (F1)

LiteLLM-proxy deploys now run a Prisma schema push against the proxy's DATABASE_URL automatically (via a new `migrate_db` Modal Function invoked by `minds env deploy`), so a fresh tier or dev env no longer requires a manual `prisma db push` step before the first virtual-key create.
