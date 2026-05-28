# Changelog consolidation: accuracy review of new bullets

The nightly changelog consolidation agent now runs a one-pass accuracy
review of the `CHANGELOG.md` bullets it just generated before opening its
PR. After committing the consolidation, it spawns a fresh-context
`general-purpose` subagent (spec in `scripts/changelog_accuracy_reviewer.md`,
relative to the repo root) that verifies each newly-added bullet against the
actual code, correcting
or removing inaccurate ones and collapsing bullets that another bullet
materially supersedes. This guards against stale or inaccurate changelog
entries.

The reviewer edits changelog files only (the code is treated as ground
truth -- it never modifies source), runs unattended (it self-reviews and
commits its own corrections rather than asking a user), and its summary
(including any cases where the code itself looked wrong) is included in the
consolidation PR's description. If the review cannot run, the consolidation
PR is still opened and the failure is noted there. The run's outcome JSON
no longer carries a freeform `notes` field; outcome detail lives in the PR
description (on success) or the run logs (on failure).
