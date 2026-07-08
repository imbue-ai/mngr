# Docs: launch-to-msg CI freeze semantics

Documented the new launch-to-msg CI pinning behavior in `docs/release.md` (step 4) and `docs/testing-overview.md`: the workflow's `commit_sha` / `template_ref` inputs accept a full 40-char SHA, branch, or tag, and are frozen to SHAs at run start — the frozen mngr SHA is what gets built and the frozen FCT SHA is what the agent is created from, so pushing to a branch after dispatch does not affect an in-flight run, and the `ref (sha)` values in the slack message are exactly what ran.
