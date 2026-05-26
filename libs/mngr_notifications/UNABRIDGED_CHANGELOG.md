# Unabridged Changelog - mngr_notifications

Full, unedited changelog entries for the `mngr_notifications` project, consolidated nightly from individual files in `libs/mngr_notifications/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-22

## Recognize the indirect "now waiting" transition

- The watcher now fires its "agent is waiting for input" notification for the new `RUNNING -> UNKNOWN -> WAITING` sequence in addition to the existing direct `RUNNING -> WAITING` transition. `AgentObserver` writes an `UNKNOWN` agent state whenever the agent's provider could not be reached during the most recent discovery attempt; an UNKNOWN agent that subsequently lands in `WAITING` (because the provider recovered or the user reached it via another path) now triggers the same desktop notification as a direct transition would.
- Internally, the watcher carries a per-agent "was RUNNING before going UNKNOWN" bit so it can bridge the indirect transition; the bit is cleared on any other transition out of UNKNOWN (notably `UNKNOWN -> RUNNING`).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.
