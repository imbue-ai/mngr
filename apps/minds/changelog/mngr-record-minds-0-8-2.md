- Recorded the minds 0.8.2 release in `docs/deploy/history/minds-v0.8.2.md`: the thorough-path cut, its stop on Apple's lapsed developer agreement and its re-freeze on the latest mngr and template, the bundle-lock check that accepted filelock 4.0.0, every launch-to-msg run and what it proved, the ROLLOVER deploys that put every user's account mail on Postmark, the bakes on both tiers, and the alpha promotion (mac and web; Linux held on 0.7.4).

- Reset `next_deploy.md` for the next release: the decisions on PR #1256 (merged, in 0.8.2) and PR #1500 (closed unmerged) and both Postmark items are discharged; the staging rehearsal, Stage F, the beta/stable promotion and the device-login check now target 0.8.2; the pool keep-list gains `minds-v0.8.2`; and a new item asks to prune or extend the 0.4.0 wire-compat snapshot before its support window ends on 2026-10-22.

- `docs/deploy/history/rollouts/postmark-transport.md` records the proven production send and the 2026-10-01 deploys that shipped PR #1480 to both tiers.

- The app-release runbook now says how to name the template checkout's branch when a second cut runs from the same mngr checkout, to re-create a release checkout made before a template history rewrite, and that a major moving only in the bundle lock is exercised only by launch-to-msg.
