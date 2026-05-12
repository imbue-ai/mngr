You are running as a nightly changelog consolidation automation inside an
ephemeral Modal sandbox. The schedule creates a fresh worktree at
`$MNGR_AGENT_WORK_DIR` with a per-run branch
(`mngr/changelog-consolidation-<timestamp>`) checked out — that is the
directory you must operate in. Execute the following steps in order,
exactly. Do not deviate. Do not ask questions.

Your **final assistant message must be a single JSON object** matching
the schema below — nothing before it, nothing after it, no markdown
code fence, no commentary. The cron framework parses your final message
to determine outcome.

```
{
  "status": "done" | "skipped-no-entries" | "failed",
  "pr_url": "<url>" | null,
  "notes": "<freeform human-readable string; multi-line ok>"
}
```

If any step fails, your final message must be a `failed` JSON object
with the failing step number and error detail in `notes`.

1. `cd "$MNGR_AGENT_WORK_DIR"`. Verify with `git rev-parse --abbrev-ref
   HEAD` that you are on a `mngr/changelog-consolidation-*` branch (not
   `HEAD`). If you are on detached HEAD, the schedule topology has
   drifted from the assumption above; emit a `failed` JSON object
   with `pwd` + branch state in `notes`.

2. Run `python3 scripts/consolidate_changelog.py`. Capture stdout. If
   stdout contains the literal string "No changelog entries", emit
   `{"status": "skipped-no-entries", "pr_url": null, "notes": ""}` and
   stop. Otherwise stdout contains a `Sections added: YYYY-MM-DD,
   YYYY-MM-DD, ...` line (newest first) — these are the date headings
   the consolidator just inserted at the top of `UNABRIDGED_CHANGELOG.md`.

3. For each date in `Sections added`, read its bullet content from
   `UNABRIDGED_CHANGELOG.md` (the section is between `## <date>` and the
   next `## ` line) and generate a few concise, human-friendly bullets.
   Each bullet MUST start with one of these Keep-a-Changelog categories
   followed by `: ` and the description, e.g.:

   ```
   - Added: Nightly changelog consolidation cron with Pacific-time dating.
   - Fixed: Race condition when two consolidation runs overlap.
   - Changed: Renamed `_get_entry_added_datetime` to use first-parent committer date.
   ```

   The allowed categories are exactly: `Added`, `Changed`, `Deprecated`,
   `Removed`, `Fixed`, `Security`. Use `Changed` as the catch-all for
   internal refactors, doc edits, or test-only tweaks that don't fit
   the other categories. One change → one bullet; merge near-duplicate
   bullets across dates if they describe the same user-visible effect.

4. In `CHANGELOG.md`, locate the `## [Unreleased]` heading (it sits
   directly below the file header — `scripts/release.py` guarantees it
   is always present after each release, and the initial one was added
   manually). If it is *not* present, the invariant has been broken;
   emit a `failed` JSON object with "missing [Unreleased] heading in
   CHANGELOG.md" in `notes` and stop. Group the bullets you generated
   in step 3 (across all dates) by category and merge them into the
   `[Unreleased]` section under `### <Category>` subheadings, in the
   canonical order: Added, Changed, Deprecated, Removed, Fixed,
   Security. Append to any existing bullets under each subheading; do
   not delete or rewrite pre-existing bullets. (`scripts/release.py`
   renames `[Unreleased]` to `[vX.Y.Z] - YYYY-MM-DD` at release time
   and inserts a fresh empty `[Unreleased]` above it, so the section
   accumulates across consolidation runs within a release window.)

5. Refinement pass on `[Unreleased]`: re-read just that section as you
   wrote it. Tighten any wordy bullets (cut filler words; keep names
   of changed APIs/files); merge bullets that describe the same
   user-visible change; confirm every bullet has a category prefix in
   the exact `- <Category>: <description>` format. Make at most one
   targeted edit per bullet — don't rewrite for the sake of rewriting.

6. Configure git: `git config user.email "bot@imbue.com"`,
   `git config user.name "Changelog Bot"`, `gh auth setup-git`.

7. Capture today's date in Pacific time: `RUN_DATE=$(TZ=America/Los_Angeles
   date +%Y-%m-%d)`. This identifies *when this consolidation run
   happened*, distinct from the per-entry `## YYYY-MM-DD` section
   headings in `UNABRIDGED_CHANGELOG.md` (which identify when each
   entry was written). `git add -A` and `git commit -m "Consolidate
   changelog entries (run <RUN_DATE>)"`.

8. Capture the current branch name with `BRANCH=$(git rev-parse
   --abbrev-ref HEAD)` and push it: `git push --set-upstream origin
   "$BRANCH"`. The schedule's auto-merge step ran `git fetch && checkout
   && merge origin/main` before this agent started, so the per-run
   branch is forked off current `origin/main` and the eventual PR diff
   contains only the consolidation commit.

9. Open a PR with `gh pr create --base main --title "Changelog
   consolidation (run <RUN_DATE>)" --body "Automated changelog
   consolidation run on <RUN_DATE>."`. Capture the URL from stdout
   into `PR_URL` while diverting stderr to a temp file, e.g.
   `PR_URL=$(gh pr create --base main --title "..." --body "..." 2>/tmp/gh_stderr)`.
   **Do not** fold stderr in via `2>&1` — `gh pr create` writes progress
   lines (e.g. "Creating pull request for X into Y in Z") to stderr
   that would mangle the captured URL. If `gh pr create` exits
   non-zero, read `/tmp/gh_stderr` and emit a `failed` JSON object
   with that stderr content in `notes`.

10. Emit your final JSON object: `{"status": "done", "pr_url":
    "<PR_URL>", "notes": "Opened PR <PR_URL> for branch <BRANCH>."}`,
    substituting the values from steps 8 and 9.
