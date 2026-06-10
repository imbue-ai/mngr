Restructured the changelog consolidation prompt
(`scripts/changelog_consolidation_prompt.md`) to produce more concise
summaries: the concise `CHANGELOG.md` bullets are now generated once per
project over all of that project's new dated sections (rather than once per
date, which created cross-date duplicates), followed by a single critical
"concision pass" that drops non-notable bullets and tightens the rest.

Fixed the nightly changelog consolidation schedule firing at 8 AM Pacific
instead of midnight. `scripts/setup_changelog_agent.sh` set the cron to
`0 8 * * *` assuming it was interpreted as UTC, but the schedule is actually
interpreted in the deploying machine's local timezone (Pacific). It now uses
`0 0 * * *` with an explicit `--timezone America/Los_Angeles`, so it fires at
midnight Pacific regardless of where the deploy runs.
