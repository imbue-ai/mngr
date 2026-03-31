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

#### Step 2: Classify the entry

After initial research, classify the entry as either **straightforward** or **ambiguous**:

- **Straightforward:** The intent is clear, the fix/change is obvious, and there's no real decision to make about *what* to do. Go directly to Step 3 (Preview).
- **Ambiguous:** There's a design decision to make, multiple possible approaches, unclear scope, or the entry questions whether something should exist at all. For these, you MUST investigate more deeply before previewing. Go to Step 2a.

#### Step 2a: Investigate ambiguous entries

For ambiguous entries, do deeper research before presenting anything to the user:

- Read the relevant code in depth (not just grep for names -- understand the actual behavior).
- Understand the relationships between the concepts mentioned (e.g., how two similar-sounding options differ).
- Identify the concrete options for how to handle the issue.

Then present the user with **options** before drafting a ticket:

- Summarize what you found (briefly).
- List the concrete options (e.g., "Option A: remove it entirely. Option B: rename to X. Option C: merge with Y.").
- Ask which direction they want to go.
- Only after the user picks a direction should you draft the issue preview.

#### Step 2b: Ask Clarifying Questions (if needed)

If there is genuine ambiguity that you cannot resolve from context, ask clarifying questions. Before asking:

- Make sure the question cannot be answered by reading the code or git history.
- Provide your best guess along with the question, so the user can just confirm rather than explain from scratch.
- Keep questions minimal -- only ask what is truly necessary.
- If you found a matching existing issue, present it and ask whether to merge, update, or create a new one.

#### Step 3: Preview the Issue

Show the user a complete preview of the GitHub issue you would create:

```
---
TITLE: <issue title -- should be of the form "Fix X by doing Y" or "Add X to Y" -- not just a problem statement, but the concrete approach>
PRIORITY: <priority:critical|high|medium|low>
SIZE: <size:xs|s|m|l|xl>
PROJECT: <project:name>  (list each on its own line if multiple)
LABELS: <other labels, comma-separated>
---

<issue body in markdown, following the structure below>

---
Original backlog entry:
> <exact original text, preserving indentation with spaces>
---
```

**Issue title guidelines:**
- The title should encode both the problem AND the solution approach, e.g. "Fix trust dialog on remote agents by auto-trusting target_path" not just "Trust dialog appears when launching remote agents".
- Keep it under ~80 characters while still being specific.

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

**Issue body structure:** Every issue body MUST use this two-section structure:

```
## Problem

<What is broken or missing, stated clearly and specifically. Include:
- The exact behavior observed (or the gap)
- Where in the code this happens (file paths, line numbers, function names)
- Why this matters (what goes wrong for the user)>

## Solution

<A single, concrete, fully-defined approach. Include:
- What specifically needs to change (which files, which logic)
- How the fix works (not just "fix it" -- describe the mechanism)
- Any edge cases or considerations the implementer should be aware of>
```

**CRITICAL: The Solution section must describe exactly ONE approach.** No "or" alternatives, no "either X or Y", no "Option A / Option B", no "we could do X, or alternatively Y". The implementer should be able to read the Solution and know exactly what to do without making any design decisions themselves. If you find yourself wanting to write "or", STOP -- you have not yet decided on the solution. Use the "investigate ambiguous entries" flow (Step 2a) to resolve the ambiguity with the user BEFORE drafting the ticket. This is the single most important rule for issue quality.

Keep it concise but specific enough that someone could implement the fix without re-doing the investigation. The goal is that an implementer reads the ticket and knows exactly what to do.

**Original text preservation:** The exact original text from the backlog file MUST appear at the bottom of the issue body in a blockquote. This is non-negotiable -- if the interpretation is wrong, the user needs to be able to see what they originally wrote.

#### Step 4: Wait for Approval

Present the preview and wait for the user to respond. Always present options as a multiple-choice list so the user can respond with a single character:

> **a)** Approve -- create the issue as shown
> **e)** Edit -- modify the title, body, or labels before creating
> **t)** Think harder -- launch a background agent to investigate this more deeply, then move to the next entry
> **s)** Skip -- move on without creating an issue for this entry
> **n)** Note and skip -- skip this entry but the user wants to leave a note (correction, context, etc.) that should be appended to the entry in the backlog file for next time
> **x)** Stop -- end the triage session (remaining entries stay in the file)

**If the user chooses Edit:** Incorporate their feedback, then show the full updated preview again and re-present the same `a/e/t/s/n/x` options. Do NOT create the issue until the user explicitly approves with `a`. This loop repeats until the user approves, skips, or stops.

**If the user chooses Note and skip:** Ask the user for their note, then append it as an indented sub-line under the entry in the backlog file (preserving the entry for future triage). Move on to the next entry.

**If the user chooses Think harder:** Launch a background sub-agent (using the Agent tool with `run_in_background: true`) to do a deep investigation of the entry. The sub-agent should:
- Read all relevant code in depth, not just grep for names.
- Understand the current behavior and why it exists.
- Identify concrete options for how to handle the issue, with pros/cons.
- Check for related issues, recent changes, and potential impacts.
- Return a summary with recommendations and questions.

While the sub-agent runs, immediately move on to the next entry in the queue. When the sub-agent completes (you will be notified), do NOT immediately present its findings. Instead, queue the results and only present them after the user has fully resolved the current entry (i.e., after they approve, skip, or stop on whatever entry is currently being discussed). Never present findings from a background agent while the user is in the middle of deciding on a different entry -- only one entry should be under discussion at a time.

#### Step 5: Create and Clean Up

Once approved:

1. **If merging into an existing issue:** Update that issue with `gh issue edit <number>` to incorporate the new information. Show the user the updated issue.
2. **If creating a new issue:** Run:
   ```bash
   gh issue create --title "<title>" --body "<body>" --label "<priority>" --label "<size>" --label "<project>" --label "<category>" --label "autotriage"
   ```
   Each label MUST be a separate `--label` flag. Never comma-separate labels in a single flag.
   The `autotriage` label MUST always be included on every issue created by this skill.
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
