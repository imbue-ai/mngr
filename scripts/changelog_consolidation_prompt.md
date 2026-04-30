You are running as a nightly changelog consolidation automation inside an
ephemeral Modal sandbox. The schedule creates a fresh worktree at
`$MNGR_AGENT_WORK_DIR` with a per-run branch
(`mngr/changelog-consolidation-<timestamp>`) checked out — that is the
directory you must operate in. The deployed-code root at `/code/project`
is on detached HEAD and is NOT the right place to commit. Execute the
following steps in order, exactly. Do not deviate. Do not ask questions.
If any step fails, capture the failure detail in `status.json` (see
step 11) and exit non-zero.

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

3. Re-base the per-run branch onto `origin/main` so the eventual PR
   contains ONLY the changelog changes — not every diff on whichever
   dev branch the cron container was deployed from. Steps:
   - Capture the current branch: `BRANCH=$(git rev-parse --abbrev-ref HEAD)`.
   - Stash the consolidation outputs (creations, modifications, and
     `changelog/*.md` deletions all in one stash):
     `git stash push -u -m consolidation -- CHANGELOG.md UNABRIDGED_CHANGELOG.md changelog/`.
   - `git fetch origin main`.
   - `git checkout -B "$BRANCH" origin/main` (resets the branch to
     `origin/main` while keeping the stashed changes apart).
   - `git stash pop`. If a conflict occurs (it shouldn't — the only files
     touched are CHANGELOG.md / UNABRIDGED_CHANGELOG.md / changelog/, all
     either appended to or deleted), abort: write `status.json` with
     `status: failed`, include the conflict output in `notes`, and exit
     non-zero. Do NOT attempt to resolve.

4. Read `UNABRIDGED_CHANGELOG.md`. Find the most recent date section
   (heading matching `## YYYY-MM-DD`). Extract the date string and the
   bullet content under it.

5. Generate a concise, human-friendly summary of that section: a few markdown
   bullets, no preamble, no trailing prose. Group related changes. Use natural
   language.

6. Insert the summary into `CHANGELOG.md` under the same date heading,
   immediately above any prior date sections (so dates remain in
   reverse-chronological order). Preserve the existing file header.

7. Configure git: `git config user.email "changelog-bot@imbue.com"`,
   `git config user.name "Changelog Bot"`, `gh auth setup-git`.

8. `git add -A` and `git commit -m "Consolidate changelog entries for <date>"`,
   substituting the date from step 4.

9. Push the per-run branch (still in `$BRANCH` from step 3) to origin:
   `git push --set-upstream origin "$BRANCH"`. The branch was already
   re-based onto `origin/main` in step 3, so the push lands a single
   consolidation commit on top of main.

10. Open a PR with `gh pr create --base main --title "Changelog
    consolidation <date>" --body "Automated changelog consolidation for
    <date>."`. Capture the URL from stdout into `PR_URL`. **Do not** fold
    stderr in via `2>&1` — `gh pr create` writes progress lines (e.g.
    "Creating pull request for X into Y in Z") to stderr that would
    corrupt `status.json` if mixed with the URL. If `gh pr create` exits
    non-zero, write `status.json` with `status: failed` and the captured
    stderr in `notes`, and exit non-zero.

11. Write `status.json` to `$MNGR_AGENT_STATE_DIR/status.json` with this
    schema (all keys required):
    - `status`: one of `"done"` (success path), `"skipped-no-entries"`
      (step 2 short-circuit), or `"failed"` (any step failed)
    - `pr_url`: string PR URL on success, else `null`
    - `notes`: freeform human-readable string. On success: short note
      like `"Opened PR <pr_url> for branch <branch>."` where `<branch>`
      is the value captured in step 3 and `<pr_url>` is from step 10.
      On failure: which step failed and the error detail. Multi-line OK.

12. Exit 0 on success, non-zero on any failure.
