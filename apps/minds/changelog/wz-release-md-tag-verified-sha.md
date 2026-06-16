Correct `apps/minds/docs/release.md`: tag the **verified** mngr SHA, not `main` HEAD.

The merge/tag steps assumed the merged `main` HEAD equals the SHA you built and verified in step 4. In practice `main` can advance past that SHA between verification and merge (unrelated PRs landing), so tagging `main` HEAD ships an unverified, vendor-mismatched tree. `release.md` now:

- **Merge the mngr release PR with a merge commit, not a squash**, so the verified SHA stays reachable on `main` as the merge parent.
- **Tag `minds-v<version>` on that verified SHA** (`GREEN_MNGR_SHA` from step 4) and on the FCT commit whose `vendor/mngr` is its archive — never `main` HEAD.
- The vendor-match check now compares against the verified SHA; a `main`-HEAD mismatch is documented as **expected drift**, not an error.
- The close-loop CI now reuses the already-verified build (the tag is the step-4 SHA), instead of repackaging.
- Step 3 (vendor refresh) now points at the `just sync-vendor-mngr` recipe instead of inline `git archive`, so the doc and the recipe stay in sync, and documents the per-user `$FCT_DIR` env var (set up front, alongside `GH_TOKEN`) so a release agent knows to point the recipe at its `forever-claude-template` checkout. No personal path is hardcoded.

Caught while cutting `minds-v0.3.1`: `main` HEAD had drifted +58 unrelated files past the verified SHA, so the tag was placed on the verified merge parent.
