# Skill authoring guide

How to write and restructure an agent skill: its `SKILL.md`, the reference files it points at, and the scripts it runs. It applies to every skill a repository owns. A skill installed from elsewhere (a marketplace, another repository, a download) keeps the shape it came with.

Two things enforce this guide, and it is the source of truth for both:

- Rules a script can decide are checked by a validator, in a repository that has one (default-workspace-template's `validate_skill.py`).
- Rules that need judgement are reviewed against this guide by the `skill_hygiene` category in `.reviewer/code-issue-categories.md`.

## Contents

- Why size and structure matter
- SKILL.md
- Step lists and contents lists
- References
- Descriptions
- Scripts
- Old patterns
- Paths
- Exemptions
- Glossary

## Why size and structure matter

An agent holds three layers of a skill, and each costs differently:

- The **catalog**: every skill's name and description, in context for the whole session.
- The **SKILL.md**: loaded whole when the skill is invoked.
- The **references**: each read only when the agent is told to.

Two things decide how much of a skill an agent is working from.

The first is how an agent reads. It does not always read a long file to its end: it reads in portions, and may stop once it has what it came for. A short file is read whole, so every rule in it is seen.

The second is compaction, which decides how much survives a long task. When Claude Code summarizes a conversation it re-attaches only the first 5,000 tokens of each invoked skill, 25,000 tokens across all of them, most recently invoked first, and it never re-reads the file. A reference the agent merely read may not come back: Claude Code re-reads at most five of the files a session read or edited, most recently modified first, and gives only the path of one over 5,000 tokens. Some other harnesses keep less of the text itself: Codex and Pi re-attach neither a skill nor a file after a compaction, so there the agent has only what the summary carried until it reads the file again.

Measured over 111 skill markdown files, this prose is 2.8 bytes per token on the current Claude tokenizer, with a 5% spread. Words and lines are poor proxies. So:

- 5,000 tokens is 14 KB, about 250 lines.
- A 500-line SKILL.md is about 10,000 tokens: its second half is gone after one compaction.

## SKILL.md

SKILL.md is the checklist an agent works from, not the manual.

- **Size.** At most 14,000 bytes for the whole file, and at most 500 lines of body.
- **Order.** Standing instructions first, then the step list, then the detail of each step. What comes first is what survives.
- **Standing instructions.** The rules that hold for the whole task ("never merge the worker's branch"), stated once, as rules and not as steps.
- **Step list.** One line per step, copyable as a checklist, in the form "Step lists and contents lists" sets. Mark each step that ends the turn.
- **Each step names its reference.** A step that needs more than a few lines says which file to read and when ("if the push is rejected, read `references/failures.md`"). An agent that has been compacted re-reads what its current step names, so a step with no pointer is a step with no detail.
- **After a compaction.** The repository's instructions file carries the general rule, once: re-read the reference the current step names, and the SKILL.md of the skill being followed first unless its whole text is still in context. A harness loads that file again after a compaction whether or not it re-attaches the skill. The rule sits near the top of that file, since some harnesses give the agent only the start of a long one. A skill adds a standing instruction of its own only for what that rule does not reach: a reference its agent follows from start to end and no step names. An instruction to re-read that sits in a reference is lost with the reference.
- **Move out** anything most runs do not need: rare paths, lookup tables, long examples, flag references, troubleshooting. Keep in what every run needs.
- **One term per concept**, and one default with an escape hatch in place of a menu of options.

## Step lists and contents lists

An agent finds its place in a long file from the list near its top.

- **A procedure has a step list**, under `## Steps`: numbered, in the order the steps run.
- **Any other long file has a contents list**, under `## Contents`: bulleted.
- **One entry per section.** Each H2 below the list has one entry, in the heading's own words: `3. Make the change` for `## Step 3: Make the change`. An entry may add one clause after them, a condition or that the step ends the turn. It never covers two sections.
- **Below a step list, every H2 is a step.** What is not a step (when not to use the skill, a term it defines) goes above the list. Choosing a value the steps use, such as a name for the work, is part of the step that first uses it.
- **The step list is the whole procedure.** An agent that did only what it names would be done, so the step that does the work is on it, not only the steps around it.
- **A heading names an action or a topic**, in the words an agent would search for ("Write the task file", "When the gateway is unreachable"). Text the agent sends the user as written goes in a fenced block, not under headings of its own.
- **When one is required.** A SKILL.md over 200 lines, and a reference over 100, has its list within its first 100 lines.

## References

A reference is any file a skill points an agent at: its own `references/` files, and files shared between skills.

- **A link is a path.** Naming a file by a path that resolves links to it: from the repository root, from the linking file's own directory or its `references/`, or, in one of the skill's own files, from the skill's directory or its `references/`. A path into a copy of the tree kept elsewhere in the repository (`<copy>/.agents/...`) links the file it copies. A filename pattern such as `op-<operation>.md` links to nothing, so list the concrete files where the choice is made.
- **Depth of two.** Every file a skill reaches is at most two links from its SKILL.md: SKILL.md links a reference, and that reference may link one file further. When a reference needs a third file, name that file from SKILL.md or from the first reference instead. A file that still cannot be reached within two links is layered wrongly: move the content, as the next rule says, and do not reword the link.
- **Links run from the specific to the general.** A skill links its own references and the shared ones. A shared reference links only the files it was split into and shared references more general than itself: it may name a skill, but no file inside one. It never names another file that builds on it, by path or by description. Content lives in the most general file whose every reader needs it. Where a general file meets something only some of its readers have, it says so in its own terms ("if your skill defines a catch-up rule, follow it"), and the specific file supplies the rest.
- **Reachability.** Every file in a skill's directory is reached from its SKILL.md, and every shared file is reached from some skill, from the repository's instructions file, or from the program that loads it. Delete a file nothing reaches.
- **A skill's files have one reader**: an agent following that skill. Nothing outside a skill links a file inside it other than its SKILL.md. When one skill needs what another holds, knowledge both need moves to a shared reference, written for any reader. A flow that should take over is reached by naming the skill, whose SKILL.md sends its own reader on: an agent finds a skill by its name, so the path to its SKILL.md is written only for a copy the catalog does not hold. What another party does is stated in this skill's own terms, without a link: that party follows its own guide.
- **The condition is at the link.** Say when to read a file where you link it. The file itself does not repeat the condition: whoever is reading it was already sent.
- **Shared references stay shared.** They live in one directory beside the skills (`.agents/shared/` beside `.agents/skills/`), and exist so one fact has one home. When one grows large, split it into a core that keeps its path and holds what every reader needs, and one file for each path only some readers take, in a directory named for the core (`deploy.md`, `deploy/rollback.md`). The core links each where its condition arises. Never copy a shared reference's content into the skills that use it.
- **A second reader has its own directory.** When a skill's lead dispatches a worker that follows a guide of its own, that guide is one reference and the files only the worker follows sit in a directory named for it (`worker.md`, `worker/validation.md`).

## Descriptions

A description is the only part of a skill an agent sees before choosing it, and all of them are in context at once.

- **Shape:** what the skill does, when to use it, when not to. Put the triggers first, in the words a user would say.
- **When not** names the neighbouring skill that takes the case instead.
- **No body content:** no steps, no file paths, no instructions on how to word a reply.
- **No first or second person:** "Publishes a snapshot...", not "Use when you want to..." and not "I can help...".
- **A skill only another agent's task invokes** says so in one line.
- **Length:** 1 to 1,024 characters, by the Agent Skills specification. Shorter is better; every skill pays for every description.

This shape is where Anthropic's, OpenAI's and agentskills.io's guidance agree, so it is not tuned to one harness.

## Scripts

- A procedure that runs the same commands every time is a script, not prose. Judgement stays in prose; the fixed sequence around it does not.
- SKILL.md tells the agent to run a script, never to read it.
- A script takes arguments, asks nothing interactively, and reports through exit codes the prose interprets.

## Old patterns

Version-bound text is anything true only for some versions: a compatibility shim, a branch for an older launcher, a one-time migration, a "may predate".

- It lives in one reference for each reader of the skill, never interleaved with the live path. The live path carries at most a one-line conditional pointer to it.
- The step that points at an item states the test for whether it applies, as something the agent can observe (a script is missing, a field is absent) and not as a version. The item does not repeat it.
- Each item carries a `CLEANUP:` marker saying what can be removed and when.
- The live path has no dates and no "currently", "new" or "not yet".

## Paths

- A path inside this repository must exist.
- A path that sends the reader to a file in this repository is written relative, to the repository root or to the file that names it. An absolute path (`/home/user/workspace/...`, `~/...`) links nothing: a file named only that way counts as unreached, and nothing checks that the path exists. Keep absolute paths for a command that has to run from another directory.
- A path that is not in this tree is written with a placeholder root: `<mngr>/apps/minds/README.md` for another repository, `<template>/.agents/skills/welcome/SKILL.md` for a repository being adopted.
- An example name is a placeholder too: `system/apps/<name>/app.toml`, not `system/apps/foo/app.toml`.
- Absolute paths, globs, and paths git ignores (generated files, a workspace's data) are not checked.

## Exemptions

A repository whose validator checks these rules may let a skill fail named checks for a time. The record is in the skill's own frontmatter (`metadata.exempt-checks`), and the repository says which reasons there are and who may write or clear one (default-workspace-template: `docs/system/skill_exemptions.md`). Three rules hold wherever there are exemptions:

- A failing check is fixed by restructuring the skill: splitting, moving and rewriting its files is always permitted. An exemption is never the answer to a failure.
- A new skill carries no exemption.
- A review skips the criteria a skill is exempt from, and judges only what the diff adds.

## Glossary

Vendor-neutral terms for writing about skills. Use these and not a harness's own names.

| Term | Meaning |
|---|---|
| skill | A directory holding a `SKILL.md` and the files it uses. |
| catalog | The names and descriptions of all skills, always in an agent's context. |
| invoke | Load a skill's SKILL.md into the conversation, by the agent's choice or a user's command. |
| harness | The program that runs the agent and loads skills for it. |
| instructions file | The repository's standing instructions to agents (`AGENTS.md`). |
| standing instruction | A rule that holds for the whole task, as opposed to a step. |
| step list | The numbered list of a procedure's steps near the top of its file. |
| reference | A file a skill points an agent at, read on demand. |
| shared reference | A reference in the shared directory beside the skills, outside any one skill's directory and used by several. |
| link | A path to a file, written where an agent will read it. |
| depth | The fewest links from a SKILL.md to a file. |
| contents list | The list near the top of a long file naming its sections. |
| compaction | The harness summarizing a long conversation to free context. |
| lead | The agent that dispatches a task to another agent and handles its report. |
| worker | The agent a lead dispatches. |
| old pattern | Text that is true only for some versions. |
| placeholder | A name in angle brackets standing for a value: `<name>`, `<mngr>`. |
| exemption | A frontmatter record that a skill fails named checks and may, for now. |
| third-party skill | A skill installed from elsewhere and kept as it came. |
