You are running as a nightly changelog consolidation automation inside an
ephemeral Modal sandbox. The schedule creates a fresh worktree at
`$MNGR_AGENT_WORK_DIR` with a per-run branch
(`mngr/changelog-consolidation-<timestamp>`) checked out — that is the
directory you must operate in. Execute the following steps in order,
exactly. Do not deviate. Do not ask questions.

Your **final assistant message must be a single JSON object** — nothing
before it, nothing after it, no markdown code fence, no commentary. The
cron framework parses your final message to determine outcome. Emit the
shape that matches what happened:

```
{"status": "done", "pr_url": "<url>"}
{"status": "skipped-no-entries"}
{"status": "failed", "notes": "<failing step number + error detail>"}
```

If any step fails, emit the `failed` shape with the failing step number
and error detail in `notes`.

Background: this repo uses an in-project changelog layout. Each project
under `libs/`, `apps/`, plus the synthetic top-level `dev/` directory,
owns three artifacts at its root: a `changelog/` directory for per-PR
entry files (`<project_dir>/changelog/<branch>.md`), a `CHANGELOG.md`
for the consolidated summary, and an `UNABRIDGED_CHANGELOG.md` for the
verbatim per-date sections. Your job is to fan each pending entry into
the right project's consolidated files.

1. `cd "$MNGR_AGENT_WORK_DIR"`. Verify with `git rev-parse --abbrev-ref
   HEAD` that you are on a `mngr/changelog-consolidation-*` branch (not
   `HEAD`). If you are on detached HEAD, the schedule topology has
   drifted from the assumption above; emit a `failed` JSON object with
   `pwd` + branch state in `notes`.

2. Run `python3 scripts/changelog_consolidate.py`. Capture stdout. If
   stdout contains the literal string "No changelog entries", emit
   `{"status": "skipped-no-entries"}` and stop.
   Otherwise stdout contains one `SECTION <project> <YYYY-MM-DD>
   [<YYYY-MM-DD> ...]` line per project the consolidator just touched. The
   dates (newest first) are the `## YYYY-MM-DD` sections it just inserted
   at the top of that project's `<project_dir>/UNABRIDGED_CHANGELOG.md`,
   where `<project_dir>` is `libs/<project>` for libs, `apps/<project>`
   for apps, and `dev/` for the synthetic dev bucket (the same directory
   that holds the project's `changelog/` entries dir).

3. For each `SECTION` line (one per project), read the bullets from *all*
   of that project's listed date sections in its
   `UNABRIDGED_CHANGELOG.md` (each section sits between its `## <date>`
   heading and the next `## ` line), pool them, and from that pooled set
   generate a few concise, human-friendly bullets for that project's
   `CHANGELOG.md`. Summarizing a project's whole pool together lets a
   single user-visible change become one bullet even when several entries
   (possibly across different days) touched it.
   Each bullet MUST start with one of these Keep-a-Changelog categories
   followed by `: ` and the description, e.g.:

   ```
   - Added: Nightly changelog consolidation cron with Pacific-time dating.
   - Fixed: Race condition when two consolidation runs overlap.
   - Changed: Renamed `_get_entry_added_datetime` to use first-parent committer date.
   ```

   The allowed categories are exactly: `Added`, `Changed`, `Deprecated`,
   `Removed`, `Fixed`, `Security`. Use `Changed` as the catch-all for
   internal refactors or doc edits that don't fit the other categories.

   `CHANGELOG.md` is a notable-only summary: if a change isn't notable,
   omit it from `CHANGELOG.md` entirely rather than forcing a bullet for
   it. The canonical example is a change that only affects tests rather
   than user-facing behavior — skip it. For a library project, public
   API changes count as user-facing: they affect consumers even when
   end-user behavior is unchanged. Major internal refactors are
   in scope too, even when in theory they leave the public surface and
   end-user behavior unchanged: in practice a large restructuring can
   introduce regressions, and a reader scanning the changelog to work out
   what might have caused a problem should be able to see it. Only minor
   or obviously no-op refactors may be omitted. If none of a project's
   entries are notable, it is fine to produce no `CHANGELOG.md` bullets
   for it at all.

   Exception for the `dev` project: its audience is the repo's own
   developers, so judge `dev` entries by developer/maintainer impact — a
   CI, build, release, or tooling change that affects how the repo is
   built, tested, or released is notable even though it isn't
   end-user-facing.

4. For each project that had at least one `SECTION` line: open that
   project's `CHANGELOG.md` (resolve `<project_dir>` as in step 2).
   Locate the `## [Unreleased]` heading (it sits directly below the
   file header). For a publishable libs/apps project it is always present
   (`scripts/release.py` guarantees it after each release, and the initial
   one is created when the project's changelog is set up); if it is *not*
   present, the invariant has been broken for that project — emit a
   `failed` JSON object with "missing [Unreleased] heading in
   <project_dir>/CHANGELOG.md" in `notes` and stop. The **`dev`** project
   is the sole exception: its changelog is date-organized and carries no
   standing `[Unreleased]` between runs (see step 9), so if
   `dev/CHANGELOG.md` has no `## [Unreleased]` heading, create an empty one
   (the line `## [Unreleased]` on its own) directly below the file's intro
   paragraph and proceed.

   Group the bullets you generated in step 3 for that project by
   category and merge them into the `[Unreleased]` section under
   `### <Category>` subheadings, in the canonical order: Added, Changed,
   Deprecated, Removed, Fixed, Security. Append to any existing bullets
   under each subheading; do not delete or rewrite pre-existing bullets.
   (For publishable projects, `scripts/release.py` renames `[Unreleased]`
   to `[vX.Y.Z] - YYYY-MM-DD` at release time and inserts a fresh empty
   `[Unreleased]` above it, so each project's section accumulates across
   consolidation runs within a release window. For `dev`, step 9 instead
   cuts this run's `[Unreleased]` into a `## <date>` section, since `dev`
   is never released.)

   Apply special scrutiny to the `Fixed` category: only keep a `Fixed`
   bullet if it seems to fix a bug that existed in a *prior* release. A
   bug that was both introduced and fixed within the current release
   window (i.e. since the last `[vX.Y.Z]` section) never reached a
   released version, so a `Fixed` entry for it is noise to the changelog
   reader -- drop it rather than listing it under `Fixed`. Use the
   project's `UNABRIDGED_CHANGELOG.md` and per-PR entries together with
   the code to judge whether the bug predates this release.

5. Concision pass: re-read just the `[Unreleased]` section of each
   `CHANGELOG.md` you touched and step back to think critically about
   what actually matters to a reader of *this* project's changelog. For
   each bullet, decide which part of the change is genuinely important
   for that audience to see -- re-applying the notable-only test from
   step 3 -- then drop any bullet that isn't notable and cut the
   secondary detail from the ones that stay, so each bullet conveys only
   what matters about the change. If two bullets still describe the same
   user-visible change, merge them. Finally, phrase every surviving
   bullet as concisely as you can: cut filler words, keep the names of
   changed APIs/files, and confirm each bullet is in the exact
   `- <Category>: <description>` format with a valid category prefix.

6. Configure git: `git config user.email "bot@imbue.com"`,
   `git config user.name "Changelog Bot"`, `gh auth setup-git`.

7. Capture today's date in Pacific time: `RUN_DATE=$(TZ=America/Los_Angeles
   date +%Y-%m-%d)`. This identifies *when this consolidation run
   happened*, distinct from the per-entry `## YYYY-MM-DD` section
   headings in each `UNABRIDGED_CHANGELOG.md` (which identify when each
   entry was written). `git add -A` and `git commit -m "Consolidate
   changelog entries (run <RUN_DATE>)"`.

8. Run a changelog accuracy review on the bullets you just added, to
   guard against stale or inaccurate entries. Spawn one or more
   `general-purpose` reviewer subagents (fresh contexts, so they review
   the bullets with eyes that did not write them), using the Task tool,
   and **partition the projects you added `[Unreleased]` bullets to in
   step 4 across them however you judge best** -- you have full
   discretion. Balance overhead against context load: a single trivial
   change that touched many packages can be reviewed by one subagent
   covering all of them; a large run, or a project with many substantial
   bullets, is better split across several subagents so no one subagent is
   overloaded. Assign each project to **exactly one** subagent (disjoint
   partitions, so no two subagents touch the same file). Spawn them **in
   parallel** (issue all the Task calls in a single batch). Give each
   subagent exactly this prompt, with the project directory or directories
   you assigned it substituted in: "Read
   `scripts/changelog_accuracy_reviewer.md` and follow its instructions
   exactly. You are assigned these project(s): `<project_dirs>`." You MUST
   explicitly wait for **all** of the subagents to finish before
   continuing. Each verifies its assigned projects' newly-added bullets
   against the actual code, corrects or removes inaccurate ones (and may
   collapse a bullet that another materially supersedes), edits only its
   assigned `CHANGELOG.md` files, and commits its own corrections (staging
   only those files). If a subagent cannot run or errors out, you may
   retry it if that seems worthwhile; either way, do NOT fail the whole
   run on its account -- the consolidation commit is still valid.

9. Date-organize the `dev` changelog. If the `dev` project gained
   `[Unreleased]` bullets this run (i.e. one of step 2's `SECTION` lines
   was for `dev`), run `python3 scripts/changelog_finalize_dev.py --date
   "$RUN_DATE"`. `dev` is never released, so this renames its transient
   `## [Unreleased]` heading to `## <RUN_DATE>` (leaving no standing
   `[Unreleased]` behind), mirroring the per-date layout of every
   project's `UNABRIDGED_CHANGELOG.md`. The script is a no-op if there is
   nothing to cut. If it changed `dev/CHANGELOG.md`, `git add -A` and
   `git commit -m "Date-organize dev changelog (run <RUN_DATE>)"`; if it
   reported nothing to do, skip the commit.

10. Capture the current branch name with `BRANCH=$(git rev-parse
   --abbrev-ref HEAD)` and push it: `git push --set-upstream origin
   "$BRANCH"`. The schedule's auto-merge step ran `git fetch && checkout
   && merge origin/main` before this agent started, so the per-run
   branch is forked off current `origin/main` and the eventual PR diff
   contains only this run's commits (the consolidation commit, any
   per-project accuracy-review correction commits, and the dev
   date-organize commit).

11. Open a PR with `gh pr create --base main`. Title: `Changelog
   consolidation (run <RUN_DATE>)`. Body: describe this automated
   changelog consolidation run (run <RUN_DATE>); what else to surface --
   e.g. anything notable the accuracy reviewers reported -- is up to you.
   Capture the PR URL from stdout into `PR_URL` while diverting stderr to
   a temp file, e.g.
   `PR_URL=$(gh pr create --base main --title "..." --body "..." 2>/tmp/gh_stderr)`.
   **Do not** fold stderr in via `2>&1` — `gh pr create` writes progress
   lines (e.g. "Creating pull request for X into Y in Z") to stderr that
   would mangle the captured URL. If `gh pr create` exits non-zero, read
   `/tmp/gh_stderr`; if the error is something you can fix (e.g. a
   malformed invocation), correct it and retry, otherwise emit a `failed`
   JSON object with that stderr content in `notes`.

12. Emit your final JSON object: `{"status": "done", "pr_url":
    "<PR_URL>"}`, substituting the PR URL from step 11.
