# launch-to-msg CI: freeze (mngr, FCT) inputs to SHAs at run start

The `minds-launch-to-msg.yml` inputs (`commit_sha` for mngr, `template_ref` for forever-claude-template) now accept a full 40-char SHA, branch, or tag, and are resolved to full SHAs exactly once, in `check_should_run`, at run start. Every downstream job consumes the frozen SHAs instead of re-resolving the raw inputs:

- The `build` job checks out and fingerprints the frozen mngr SHA (previously it re-resolved the input ref at checkout time, after the skip-check had already fingerprinted a possibly different commit).

- Agent creation uses the frozen FCT SHA (`MINDS_WORKSPACE_BRANCH` now gets the SHA, not the ref name). Previously the raw ref was re-resolved at clone time, ~15-45 min after the pair-key fingerprint, so a `template_ref=main` run could test a different FCT commit than the one recorded in the green marker and slack message. The stale comment claiming the binary rejects SHAs predated mngr `02bb71b44`, which made `clone_git_repo` fetch branch / tag / SHA uniformly.

- The `launch_to_msg` job's FCT resolve step no longer re-resolves; it reports the frozen pin.

The slack message and step summaries keep the `ref (sha)` format; those SHAs are now guaranteed to be exactly what was built and run. Caveats documented in the input descriptions: SHAs must be full 40-hex and reachable from some ref, and FCT-SHA creates need a binary built from mngr `02bb71b44` (2026-06-11) or later.
