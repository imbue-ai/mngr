Hardened suspicious edge-case handling in the antigravity plugin:

- `merge_trusted_workspace` now raises `UserInputError` on a non-list `trustedWorkspaces` value instead of silently coercing it to a fresh single-entry array, so an unknown/hand-edited schema is surfaced rather than overwritten (consistent with the existing shape check used during provisioning).
- Documented the previously-implicit defaults in the common-transcript converter: a missing tool-result `status` is intentionally treated as success, and non-string `PLANNER_RESPONSE` content degrades to empty text rather than crashing the converter.
- Documented why the worktree-of-trusted-source check guards against the source path coinciding with the workspace-symlink path.
