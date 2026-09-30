- Recorded the minds 0.8.1 release in `docs/deploy/history/minds-v0.8.1.md`: the thorough-path cut and its re-pin to the latest mngr and template, every launch-to-msg run and what it proved, the ROLLOVER deploys and bakes on both tiers (including the production box that failed every attempt at one slice ordinal), and the alpha promotion (mac and web; Linux held on 0.7.4).

- Reset `next_deploy.md` for the next release: PR #1357 is discharged (closed on purpose), the Stage F upgrade check and the beta/stable promotion now target 0.8.1, the pool keep-list gains `minds-v0.8.1` and `minds-v0.8.0`, and two items are new before 0.8.1 goes to beta or stable: decide on PR #1500, which 0.8.1 does not carry, and run the staging rehearsal 0.8.1 skipped. A third new item gates the next bake on production box `feb11eae`, whose slice ordinal 3 failed every 0.8.1 attempt.

- The pool-hosts runbook now says which audit fields are per box and which are top-level in `server-audit`'s JSON, and that `status` comes from `server-list`.

- The app-release runbook now says that a launch-to-msg dispatch's `-r` picks the workflow and e2e harness while `commit_sha` picks the binary, and lists two more launch-to-msg failure modes: the post-test cleanup's "survived cleanup" and "no backend login URL after 120s".

- Updated the `next_deploy.md` Postmark item after staging took #1409 (`20260930T184917Z`): it now asks for a real Postmark send proven on staging, `studio.imbue.com` verified in Postmark, and a decision on #1480 before the production deploy, and lists the deploy-shell overrides to unset.
