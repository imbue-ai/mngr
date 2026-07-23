---
name: post-pr-to-slack
description: |
  Post a one-line PR announcement to #project-minds-internal-product, and mark
  it :merged: when the PR merges. Use whenever you open or meaningfully adjust
  a PR in this repo.
compatibility: |
  Requires latchkey with a working Slack credential, plus `jq` and `gh`. Run
  `scripts/slack_pr.sh check` to verify; see the latchkey skill to set it up.
argument-hint: [pr-number | pr-url] [#channel]
---

# Post PR to Slack

Announce this repo's pull requests in `#project-minds-internal-product` with a
single standalone line, then mark each announcement `:merged:` once its PR
merges. This file covers *when* to post and *what* the message should say; the
Slack mechanics (channel lookup, posting, reacting) live in
[`scripts/slack_pr.sh`](scripts/slack_pr.sh).

The skill is agent-agnostic — it depends only on `latchkey`, `jq`, `gh`, and
`bash`, not on any agent-specific tooling.

## When to run

- **When you open a PR for this repo.** After `gh pr create --base main`
  succeeds, post to `#project-minds-internal-product`. This is a standing
  instruction — just post; see [Authorization](#authorization).
- **When you meaningfully adjust an open PR** — a force-push that changes the
  approach, a scope change, or a retitle. Reply in the existing thread; don't
  start a new top-level post. Skip trivial edits (typo fixes, rebases, minor doc
  tweaks). If unsure, ask the user.
- **When a PR merges** (you observe it, or the user says so). Add a `:merged:`
  reaction to the original post. Do *not* post a separate "merged" message — the
  reaction is the convention.

## Inputs

- **Channel** — `#project-minds-internal-product` for this repo's PRs unless
  the user says otherwise.
- **PR URL** — infer from git when not given:
  - current branch: `gh pr view --json url -q .url`
  - a specific PR: `gh pr view <number> --json url -q .url`
- **One-line description** — draft from the PR title/body (see
  [Drafting](#drafting-the-one-liner)). If the PR already has a crisp one-liner
  (its title, or a `## Summary` bullet), reuse it verbatim so Slack and the PR
  stay in lockstep.

> The only PR-host-specific step is fetching the URL with `gh`. On a GitLab repo,
> swap in `glab mr view --output json | jq -r .web_url`; everything else is the
> same.

## Setup

Point a variable at the helper script and confirm Slack is reachable, once per
session:

```bash
SLACK=.claude/skills/post-pr-to-slack/scripts/slack_pr.sh
"$SLACK" check          # prints "slack ok: team '…' as '…'", or says how to fix auth
```

If the check fails, run `latchkey services info slack` and follow its setup (a
browser login if supported, otherwise `latchkey auth set`). The script puts an
nvm-installed `latchkey` back on `PATH` itself; if `check` still reports it
missing, install it with `npm install -g latchkey`.

## Steps — initial post

1. **Read recent channel history** to match the house style (terse vs.
   emoji-led, where the URL goes, capitalization), then **draft the line** per
   [Drafting](#drafting-the-one-liner):

   ```bash
   "$SLACK" history '#project-minds-internal-product'
   ```

2. **Post it** and capture the message timestamp (`ts`). The text is sent
   verbatim, so include the PR URL — Slack unfurls it into a preview:

   ```bash
   TEXT="Fix login crash when the token contains a colon  $PR_URL"
   TS=$("$SLACK" post '#project-minds-internal-product' "$TEXT")
   echo "posted ts=$TS"
   ```

   `slack_pr.sh` builds the JSON with `jq`, so backticks, quotes, or `&` in the
   text are safe.

3. **Report** the channel and `ts` back to the user, and remember the `ts` — the
   merge-time `:merged:` step needs it.

## Steps — adjusting an open PR

If a post for this PR already exists (use the `ts` you remembered, or find it),
reply in its thread — don't post a new top-level message for the same PR, which
splits the discussion:

```bash
TS=$("$SLACK" find-ts '#project-minds-internal-product' "$PR_URL")
"$SLACK" reply '#project-minds-internal-product' "$TS" "Revised approach: now …"
```

## Steps — PR merged

```bash
TS=$("$SLACK" find-ts '#project-minds-internal-product' "$PR_URL")
"$SLACK" done '#project-minds-internal-product' "$TS"
```

`find-ts` matches the bare URL even when Slack has wrapped it in `<…>`. `done` is
idempotent — an already-present `:merged:` counts as success. Don't post anything
else; the reaction *is* the "merged" signal.

## Drafting the one-liner

The audience is teammates and future reviewers skimming the channel. The line
must convey the PR's **one key outcome** and **read standalone** — no thread,
repo, or commit-history context required. Lead with a verb in the channel's
imperative style ("Fix X", "Migrate Y", "Speed up Z"). Draft it directly; if
your harness lets you delegate prose to a smaller/faster model, you may, but
it's not required.

Check the draft against these guardrails:

- **Outcome, not process.** Describe what the PR achieves in the codebase as it
  now exists — not the bootstrap, onboarding, or workflow that produced it.
- **Timeless.** No "follow-up commit does Y", "next we'll…", or "still to come".
  Frame it as if every commit on the branch has already landed and is the new
  normal. A channel post is a record, not a roadmap or a per-commit changelog.
- **One sentence, one outcome.** No parenthetical hedging in a secondary change.
  If a second change is genuinely co-equal, that's a sign it should be its own PR.
- **Plain, not hype.** "Improve performance" — not "Massive perf win!" — unless
  the channel actually talks that way.
- Ends with the PR URL; no ticket IDs unless the channel uses them.

## Authorization

The `#project-minds-internal-product` post-and-`:merged:` flow is
**pre-authorized** for this repo's PRs — this skill living in the repo *is* the
standing instruction. Don't pause to ask "should I post this?" after
`gh pr create` succeeds; just post. Posting and reacting are still visible to
others, so **do** ask before any scope expansion: a different channel,
@-mentioning people, a reaction other than `:merged:`, or posting for a PR in
some other repo.

## Notes

- **Re-resolve the channel ID each invocation.** Looking up by name tolerates
  renames; a cached ID can go stale across sessions.
- **Track the `ts` of your initial post** in conversation memory so the
  merge-time reaction doesn't have to scan history.
- **Raw API / fallback.** Every call routes through `latchkey curl` against the
  Slack Web API; the exact endpoints and payloads are in
  [`scripts/slack_pr.sh`](scripts/slack_pr.sh) if you ever need to run them by
  hand.
