# Changelog consolidation: accuracy review of new bullets

The nightly changelog consolidation agent now runs a one-pass accuracy
review of the `CHANGELOG.md` bullets it just generated before opening its
PR. After committing the consolidation, it spawns a fresh-context
`general-purpose` subagent (spec in
[`scripts/changelog_accuracy_reviewer.md`](../scripts/changelog_accuracy_reviewer.md))
that verifies each newly-added bullet against the actual code, correcting
or removing inaccurate ones and collapsing bullets that another bullet
materially supersedes. This guards against stale or inaccurate changelog
entries.

The reviewer edits changelog files only (the code is treated as ground
truth -- it never modifies source), runs unattended (it self-reviews and
makes its own correction commit rather than asking a user), and its summary
(including any cases where the code itself looked wrong) is folded into the
run's outcome JSON `notes`. If the review cannot run, the consolidation PR
is still opened and the failure is noted.
