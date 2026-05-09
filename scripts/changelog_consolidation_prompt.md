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
   next `## ` line) and generate a concise, human-friendly summary as a
   few markdown bullets — no preamble, no trailing prose. Group related
   changes within a date. Use natural language.

4. Insert each per-date summary into `CHANGELOG.md` under a `## <date>`
   heading, in the same newest-first order as `Sections added`,
   immediately above any pre-existing date sections (so dates remain in
   reverse-chronological order). Preserve the existing file header.

5. Configure git: `git config user.email "bot@imbue.com"`,
   `git config user.name "Changelog Bot"`, `gh auth setup-git`.

6. Capture today's date in Pacific time: `RUN_DATE=$(TZ=America/Los_Angeles
   date +%Y-%m-%d)`. This identifies *when this consolidation run
   happened*, distinct from the per-entry `## YYYY-MM-DD` section
   headings (which identify when each entry was written). `git add -A`
   and `git commit -m "Consolidate changelog entries (run <RUN_DATE>)"`.

7. Capture the current branch name with `BRANCH=$(git rev-parse
   --abbrev-ref HEAD)` and push it: `git push --set-upstream origin
   "$BRANCH"`. The schedule's auto-merge step ran `git fetch && checkout
   && merge origin/main` before this agent started, so the per-run
   branch is forked off current `origin/main` and the eventual PR diff
   contains only the consolidation commit.

8. Open a PR with `gh pr create --base main --title "Changelog
   consolidation (run <RUN_DATE>)" --body "Automated changelog
   consolidation run on <RUN_DATE>."`. Capture the URL from stdout
   into `PR_URL` while diverting stderr to a temp file, e.g.
   `PR_URL=$(gh pr create --base main --title "..." --body "..." 2>/tmp/gh_stderr)`.
   **Do not** fold stderr in via `2>&1` — `gh pr create` writes progress
   lines (e.g. "Creating pull request for X into Y in Z") to stderr
   that would mangle the captured URL. If `gh pr create` exits
   non-zero, read `/tmp/gh_stderr` and emit a `failed` JSON object
   with that stderr content in `notes`.

9. Emit your final JSON object: `{"status": "done", "pr_url":
   "<PR_URL>", "notes": "Opened PR <PR_URL> for branch <BRANCH>."}`,
   substituting the values from steps 7 and 8.
