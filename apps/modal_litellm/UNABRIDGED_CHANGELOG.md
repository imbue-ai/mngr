# Unabridged Changelog - modal_litellm

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/modal_litellm/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

- ``modal_litellm``'s README + module docstring drop the wrong
  ``/anthropic`` suffix from the documented ``ANTHROPIC_BASE_URL`` --
  the Anthropic SDK appends ``/v1/messages`` itself, which lands on
  LiteLLM's native route that already accepts the Anthropic request
  shape. (F1)

LiteLLM-proxy deploys now run a Prisma schema push against the proxy's DATABASE_URL automatically (via a new `migrate_db` Modal Function invoked by `minds env deploy`), so a fresh tier or dev env no longer requires a manual `prisma db push` step before the first virtual-key create.
