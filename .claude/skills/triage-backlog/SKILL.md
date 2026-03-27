---
name: triage-backlog
description: Interactively triage the user's local engineering backlog file into GitHub issues. Use when the user wants to process their raw thought notes / ticket backlog into proper GitHub issues.
---

# Triage Engineering Backlog

This skill guides you through an interactive, interruptible process of converting the user's raw engineering thoughts into well-formed GitHub issues. The user keeps a file of informal notes -- short phrases, half-formed ideas, indented sub-thoughts -- and this skill helps turn each one into a proper tracked issue (or merge it into an existing one).

## Input Format

The backlog file contains entries in this format:

```
do the whole "schema.json" thing for the events
make claude skill for mngr
    oooh, what happens to those in the namespace
    esp useful for modal
    could power a slack bot
create a mngr_pi_mind plugin
```

- Each **unindented** line is a distinct task/idea.
- **Indented** lines immediately below are sub-thoughts, details, or elaborations belonging to the task above them.
- The text is informal, abbreviated, and context-dependent. Interpreting it correctly requires understanding the codebase, recent git history, and surrounding entries.

## Process

### Phase 1: Load Context

Before touching any entries, build up the context you will need to interpret them:

1. **Read the backlog file.** Parse it into a list of entries, where each entry is the unindented line plus any indented lines below it.
2. **Read recent git history.** Run `git log --oneline -30` to understand what has been worked on recently.
3. **Scan the codebase.** Read top-level README, project READMEs, and any files that seem relevant based on a quick scan of the entries. The goal is to be able to interpret the user's shorthand.
4. **Load existing GitHub issues.** Run `gh issue list --state open --json number,title,body,labels --limit 200` so you can check for duplicates throughout the process.

### Phase 2: Prioritize

Scan through **all** entries and produce a prioritized ordering. Present this ordering to the user before starting to process individual entries. The ordering should follow these principles:

- **Tiny, easy, important fixes first.** A one-line config change that fixes a real problem is highest priority.
- **Important improvements next.** Things that unblock other work or fix real pain points.
- **New features and big ideas last.** These need the most discussion and are lowest urgency.
- **Already-done items noted.** If an entry looks like it has already been completed (based on git history or current code), flag it for removal.

Present the prioritized list to the user as a numbered list with a brief note on each explaining your reasoning. Ask if they want to reorder anything before proceeding.

### Phase 3: Process Entries One at a Time

For each entry (in priority order), follow this cycle:

#### Step 1: Interpret and Research

- Read the entry carefully, including any indented sub-thoughts.
- Search the codebase for relevant code, files, and patterns mentioned or implied by the entry.
- Check the git log for recent related changes.
- Cross-reference against the loaded GitHub issues to see if a matching issue already exists.
- Think hard about what the user likely meant. Consider the surrounding entries for additional context.

#### Step 2: Ask Clarifying Questions (if needed)

If there is genuine ambiguity that you cannot resolve from context, ask clarifying questions. Before asking:

- Make sure the question cannot be answered by reading the code or git history.
- Provide your best guess along with the question, so the user can just confirm rather than explain from scratch.
- Keep questions minimal -- only ask what is truly necessary.
- If you found a matching existing issue, present it and ask whether to merge, update, or create a new one.

#### Step 3: Preview the Issue

Show the user a complete preview of the GitHub issue you would create:

```
---
TITLE: <issue title>
PRIORITY: <priority:critical|high|medium|low>
SIZE: <size:xs|s|m|l|xl>
PROJECT: <project:name>  (list each on its own line if multiple)
LABELS: <other labels, comma-separated>
---

<issue body in markdown>

---
Original backlog entry:
> <exact original text, preserving indentation with spaces>
---
```

**Label selection:** Every issue MUST have exactly one priority label, one size label, and at least one project label. Add category labels as appropriate.

*Priority (required -- exactly one):*
- `priority:critical` -- Blocking other work or actively broken. Drop everything.
- `priority:high` -- Important and should be done soon. Next up after critical items.
- `priority:medium` -- Should be done but not urgent. Typical backlog work.
- `priority:low` -- Nice to have. Do when there's slack or it becomes more important.

*Size (required -- exactly one):*
- `size:xs` -- Trivial, a few minutes. Config tweak, typo, one-line fix.
- `size:s` -- Small, under an hour. Well-scoped, minimal risk.
- `size:m` -- Medium, a few hours. Some design decisions or multi-file changes.
- `size:l` -- Large, roughly a day. Significant feature or refactor.
- `size:xl` -- Extra large, multiple days. Major feature, cross-cutting changes, or exploration needed.

*Project (required -- at least one):*

Every issue must be tagged with the sub-project it belongs to. Use the `project:<name>` labels, which correspond to directories under `apps/` and `libs/`. If an issue spans multiple projects, apply multiple project labels. Determine the correct project by looking at which code the task would touch. For truly repo-wide concerns (CI config, root-level tooling, cross-cutting changes), use `project:repo`.

To see the current list of project labels, run: `gh label list --search "project:" --json name --limit 100`

*Category (at least one recommended):*
- `bug` -- Something is broken or behaving incorrectly.
- `enhancement` -- New capability or feature.
- `refactor` -- Code restructuring with no behavior change.
- `documentation` -- Docs improvements.
- `infrastructure` -- CI/CD, tooling, build system, deployment, dev environment.
- `inconsistencies` -- Code inconsistencies that should be standardized.
- `idea` -- Exploratory, needs more thought before it is actionable.
- `blocked` -- Waiting on an external dependency or decision (add alongside another category).

Also consider whether `good first issue` applies (small, self-contained, well-defined).

**Issue body guidelines:**
- Write a clear description of what needs to be done and why.
- Include relevant context (file paths, function names, related systems) that you discovered during research.
- If there are open questions or design decisions, note them.
- If the entry had indented sub-thoughts, incorporate them as context or as a "Notes" section.
- Keep it concise but complete enough that someone could pick it up without additional context.

**Original text preservation:** The exact original text from the backlog file MUST appear at the bottom of the issue body in a blockquote. This is non-negotiable -- if the interpretation is wrong, the user needs to be able to see what they originally wrote.

#### Step 4: Wait for Approval

Present the preview and wait for the user to:
- **Approve** -- create the issue as shown.
- **Edit** -- modify the title, body, or labels before creating.
- **Skip** -- move on without creating an issue for this entry.
- **Stop** -- end the triage session (remaining entries stay in the file).

#### Step 5: Create and Clean Up

Once approved:

1. **If merging into an existing issue:** Update that issue with `gh issue edit <number>` to incorporate the new information. Show the user the updated issue.
2. **If creating a new issue:** Run:
   ```bash
   gh issue create --title "<title>" --body "<body>" --label "<priority>" --label "<size>" --label "<project>" --label "<category>"
   ```
   Each label MUST be a separate `--label` flag. Never comma-separate labels in a single flag.
   Report the created issue number and URL.
3. **Remove the entry from the backlog file.** Delete the unindented line and all its indented sub-lines from the file. Be precise -- only remove the exact entry that was just processed.
4. **Move on to the next entry** in priority order.

### Phase 4: Wrap Up

When all entries are processed (or the user says to stop):

- Report a summary: how many issues were created, how many were merged into existing issues, how many were skipped, and how many remain in the file.
- If there are remaining entries, remind the user they can resume later.

## Important Rules

- **Preserve original text exactly.** Every created issue must contain the user's original shorthand in a blockquote at the bottom. Use `>` blockquote formatting, and preserve indentation by converting leading whitespace to non-breaking spaces or using a code block.
- **One entry at a time.** Never batch-create issues. The user must approve each one individually.
- **Interruptible.** The user can stop at any point. Entries that have not been processed remain in the file unchanged.
- **No fabrication.** If you cannot figure out what an entry means, say so honestly. Do not invent an interpretation and present it as certain.
- **Check for duplicates thoroughly.** Search existing issues by keyword, not just exact title match. If something similar exists, surface it.
- **Do not create labels.** Only use labels that already exist on the repo. If none of the existing labels fit well, omit labels rather than creating new ones (but mention this to the user so they can create one if desired).
- **File edits must be surgical.** When removing an entry from the backlog file, only remove that specific entry. Do not reformat, reorder, or otherwise modify the rest of the file.
