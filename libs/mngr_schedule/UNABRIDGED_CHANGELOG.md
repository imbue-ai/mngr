# Unabridged Changelog - mngr_schedule

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/mngr_schedule/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Bumped pinned `modal` dependency from 1.3.1 to 1.4.3 to stay in sync with the rest of the monorepo.

## 2026-05-14

`mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent that lives inside the cron-runner's local provider (i.e. inside the ephemeral Modal container). Previously the deploy machine could not reach into the container to observe or destroy the agent, so verify failed for that configuration. Verification now runs inside the container itself and reports the result back to the deploy machine over a structured sentinel line.
