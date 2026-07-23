---
name: address-pr-comments
description: Address review comments on a pull request -- apply CLAUDE:/SCULPTOR:-prefixed instructions, and critically evaluate feedback from automated reviewers (Vet, Copilot, or any bot)
disable-model-invocation: true
allowed-tools: Bash(gh:*), Bash(git:*), Glob, Grep, Read, Edit, Write
argument-hint: [pr-number]
---

# Address PR Comments

Fetch comments from a pull request and act on the ones directed at an agent.
There are two kinds, and they get different treatment:

1. **Directed instructions** — comments prefixed with `CLAUDE:` or `SCULPTOR:`
   (case-insensitive). These are a human steering the agent; apply them.
2. **Automated reviewer feedback** — comments authored by an AI or automated
   system: Vet, GitHub Copilot, or any bot account. These are suggestions, not
   instructions; validate them critically before acting.

Plain human discussion with neither prefix nor a bot author is out of scope —
leave it alone.

## Instructions

1. Fetch the PR's comments via `gh` (see "Fetching comments" below). If a PR
   number is provided, use it. Otherwise, resolve the current branch's PR with
   `gh pr view --json number`.

2. Partition the comments (including discussion notes and inline code
   comments) into the two kinds above, and drop everything else:
   - **Directed instructions**: body starts with `CLAUDE:` or `SCULPTOR:`
     (case-insensitive).
   - **Automated feedback**: the author is an automated system — `.user.type`
     is `"Bot"`, the login ends in `[bot]` (e.g. `github-actions[bot]`,
     `copilot-pull-request-reviewer[bot]`), or the author is a known automated
     reviewer such as Vet. Judge by the author, not the wording.

3. **For each directed instruction:**
   - Read the referenced file (if it's an inline comment on a specific line)
   - Understand what change is being requested
   - Apply the requested change to the local codebase

4. **For each piece of automated feedback, evaluate before applying.**
   Automated reviewers lack full context: they misfire on intentional
   patterns, house conventions, and constraints that live outside the diff.
   Do not follow them blindly:
   - Verify the claim against the actual code — read the file and enough
     surrounding context to know whether the issue is real.
   - Check it against the repo's own rules and conventions (CLAUDE.md, the
     style guide, existing patterns nearby) and against the goals of this PR.
   - Then act on your judgment: apply it if it holds up, skip it if it is a
     false positive or conflicts with the repo's conventions or the PR's
     intent, and defer to the user if it is a real trade-off you cannot settle
     from the code alone.

5. **Important restrictions:**
   - Do NOT push any code
   - Do NOT reply to the comments on GitHub
   - Only make local changes

6. **Summarize at the end**, listing every comment you considered and its
   disposition: applied, skipped (with the reason), or needs a decision from
   the user.

## Fetching comments

Fetch the PR's comments — both inline review comments and general discussion —
with `gh`:

- Inline review comments: `gh api --paginate "repos/{owner}/{repo}/pulls/<N>/comments"` — each has `.body`, `.path`, `.line` (or `.original_line`), and `.user` (`.login`, `.type`). `gh` substitutes `{owner}`/`{repo}` for the current repo.
- General discussion: `gh api --paginate "repos/{owner}/{repo}/issues/<N>/comments"`.
- PR reviews (top-level review bodies, where some bots put their findings): `gh api --paginate "repos/{owner}/{repo}/pulls/<N>/reviews"`.

**Important:** the comment JSON can be large and may get truncated — save it to
a temp file first, then parse with `jq` in a **separate** command (not chained
with `&&`) to avoid shell-parsing issues with the jq expression.

GitHub's REST comments have no resolved flag, so rely on the diff to see what's
already done.
