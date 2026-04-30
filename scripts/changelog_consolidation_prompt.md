You are running as a nightly changelog consolidation automation inside an
ephemeral Modal sandbox. The schedule creates a fresh worktree at
`$MNGR_AGENT_WORK_DIR` with a per-run branch
(`mngr/changelog-consolidation-<timestamp>`) checked out — that is the
directory you must operate in. Execute the following steps in order,
exactly. Do not deviate. Do not ask questions. If any step fails,
capture the failure detail in `status.json` (see step 10) and exit
non-zero.

1. `cd "$MNGR_AGENT_WORK_DIR"`. Verify with `git rev-parse --abbrev-ref
   HEAD` that you are on a `mngr/changelog-consolidation-*` branch (not
   `HEAD`). If you are on detached HEAD, that means the schedule
   topology has drifted from the assumption above; write `status.json`
   with `status: failed` and the captured `pwd` + branch state in
   `notes`, then exit non-zero.

2. Run `python3 scripts/consolidate_changelog.py`. Capture stdout. If stdout
   contains the literal string "No changelog entries", write `status.json`
   with `{"status": "skipped-no-entries", "pr_url": null, "notes": ""}` and
   exit 0.

3. Read `UNABRIDGED_CHANGELOG.md`. Find the most recent date section
   (heading matching `## YYYY-MM-DD`). Extract the date string and the
   bullet content under it.

4. Generate a concise, human-friendly summary of that section: a few markdown
   bullets, no preamble, no trailing prose. Group related changes. Use natural
   language.

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
   that would corrupt `status.json` if mixed with the URL. If `gh pr
   create` exits non-zero, read `/tmp/gh_stderr` and write `status.json`
   with `status: failed` and that stderr content in `notes`, then exit
   non-zero.

10. Write `status.json` to `$MNGR_AGENT_STATE_DIR/status.json` with this
    schema (all keys required):
    - `status`: one of `"done"` (success path), `"skipped-no-entries"`
      (step 2 short-circuit), or `"failed"` (any step failed)
    - `pr_url`: string PR URL on success, else `null`
    - `notes`: freeform human-readable string. On success: short note
      like `"Opened PR <pr_url> for branch <branch>."` where `<branch>`
      is the value captured in step 8 and `<pr_url>` is from step 9.
      On failure: which step failed and the error detail. Multi-line OK.

11. Exit 0 on success, non-zero on any failure.
