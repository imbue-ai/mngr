You are running as a nightly changelog consolidation automation inside an
ephemeral Modal sandbox. Working directory: `/code/project`. Execute the
following steps in order, exactly. Do not deviate. Do not ask questions. If
any step fails, capture the failure detail in `status.json` (see step 10) and
exit non-zero.

1. `cd /code/project`.

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

8. Create a fresh branch named
   `mngr/changelog-consolidation-$(date -u +%Y-%m-%d-%H-%M-%S)` (use a real
   shell command to produce the timestamp -- do not hardcode). Push it to
   origin with `--set-upstream`.

9. Do NOT run `gh pr create`. PR creation is intentionally disabled until
   these scripts have landed on `main` (chicken-and-egg: opening real PRs
   from a dev-branch consolidation would spam the repo). To re-enable PR
   creation later, replace this step with: `gh pr create --base main
   --title "Changelog consolidation <date>" --body "Automated changelog
   consolidation for <date>."`, capturing stdout-only into a `pr_url`
   variable (stderr has progress lines that would corrupt the JSON below
   if folded in via `2>&1`). See the BEFORE-MERGE TODO at the bottom of
   this prompt.

10. Write `status.json` to `$MNGR_AGENT_STATE_DIR/status.json` with this
    schema (all keys required):
    - `status`: one of `"done"` (success path), `"skipped-no-entries"`
      (step 2 short-circuit), or `"failed"` (any step failed)
    - `pr_url`: string PR URL if a PR was created, else `null` (currently
      always `null` since step 9 is disabled)
    - `notes`: freeform human-readable string. On success: short note
      like `"Pushed branch mngr/changelog-consolidation-<timestamp>; PR
      creation disabled."` On failure: which step failed and the error
      detail. Multi-line OK.

11. Exit 0 on success, non-zero on any failure.

# BEFORE-MERGE TODOs

These two changes must be made before this PR (the changelog system itself)
lands on `main`, but cannot be tested end-to-end until then:

1. **Switch step 7's commit base to `origin/main`** so consolidation PRs
   contain ONLY changelog changes, not every diff on the dev branch the
   container was deployed from. Sketch: between steps 2 and 5, stash the
   resulting `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` / changelog/
   deletions; `git fetch origin main && git checkout -B "$BRANCH"
   origin/main`; re-apply the stashed changes; then continue from step 5.

2. **Re-enable real PR creation in step 9** as described above. Capture
   stdout-only for the `pr_url` value (stderr has progress lines that
   would corrupt status.json if folded in via `2>&1`).

Both are blocked on these scripts being on `main`: (1) would branch off a
main that doesn't yet contain `consolidate_changelog.py`, and (2) would
spam real PRs on every deploy-trigger cycle during iteration.
