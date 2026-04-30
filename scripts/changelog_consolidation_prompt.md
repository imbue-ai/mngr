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
   stop.

3. Read `UNABRIDGED_CHANGELOG.md`. Find the most recent date section
   (heading matching `## YYYY-MM-DD`). Extract the date string and the
   bullet content under it.

4. Generate a concise, human-friendly summary of that section: a few
   markdown bullets, no preamble, no trailing prose. Group related
   changes. Use natural language.

5. Insert the summary into `CHANGELOG.md` under the same date heading,
   immediately above any prior date sections (so dates remain in
   reverse-chronological order). Preserve the existing file header.

6. Configure git: `git config user.email "changelog-bot@imbue.com"`,
   `git config user.name "Changelog Bot"`, `gh auth setup-git`.

7. `git add -A` and `git commit -m "Consolidate changelog entries for <date>"`,
   substituting the date from step 3.

8. Capture the current branch name with `BRANCH=$(git rev-parse
   --abbrev-ref HEAD)` and push it: `git push --set-upstream origin
   "$BRANCH"`. The schedule's `--branch` flag already created this
   branch off the deployed-code HEAD; once the changelog scripts ship
   on `main`, every cron deploy will be from main, so the branch's
   parentage is automatically `origin/main` and the eventual PR diff
   contains only the consolidation commit.

9. Open a PR with `gh pr create --base main --title "Changelog
   consolidation <date>" --body "Automated changelog consolidation for
   <date>."`. Capture the URL from stdout into `PR_URL` while diverting
   stderr to a temp file, e.g.
   `PR_URL=$(gh pr create --base main --title "..." --body "..." 2>/tmp/gh_stderr)`.
   **Do not** fold stderr in via `2>&1` — `gh pr create` writes progress
   lines (e.g. "Creating pull request for X into Y in Z") to stderr
   that would mangle the captured URL. If `gh pr create` exits
   non-zero, read `/tmp/gh_stderr` and emit a `failed` JSON object
   with that stderr content in `notes`.

10. Emit your final JSON object: `{"status": "done", "pr_url":
    "<PR_URL>", "notes": "Opened PR <PR_URL> for branch <BRANCH>."}`,
    substituting the values from steps 8 and 9.
