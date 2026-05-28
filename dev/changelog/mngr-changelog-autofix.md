# Changelog consolidation: accuracy review of new bullets

The nightly changelog consolidation agent now reviews the `CHANGELOG.md`
bullets it just generated for factual accuracy against the code, before
opening its PR. After committing the consolidation, it spawns one or more
fresh-context `general-purpose` reviewer subagents (spec in
`scripts/changelog_accuracy_reviewer.md`, relative to the repo root) and
partitions the projects that gained new bullets across them at its
discretion -- so a trivial change touching every package needn't spawn a
reviewer per package -- running them in parallel. Each verifies its
assigned projects' newly-added bullets against the actual code, correcting
or removing inaccurate ones and collapsing bullets that another bullet
materially supersedes. This guards against stale or inaccurate changelog
entries.

Each reviewer edits only the `CHANGELOG.md` files of its assigned projects
(the code is treated as ground truth -- reviewers never modify source) and
commits its own corrections, staging only those files so the parallel
reviewers don't clobber each other. Reviewers run unattended -- they self-review rather than
asking a user. Their summaries (including any cases where the code itself
looked wrong) are collected into the consolidation PR's description; if a
project's review cannot run, the PR is still opened and that failure is
noted. The run's outcome JSON keeps a freeform `notes` field only for failed
runs (which have no PR to carry detail); successful runs put their detail in
the PR description instead.
