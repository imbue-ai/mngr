# Skill authoring guide

How to write and restructure an agent skill: its `SKILL.md`, the reference files it points at, and the scripts it runs. It applies to every skill a repository owns. A skill installed from elsewhere (a marketplace, another repository, a download) keeps the shape it came with.

Two things enforce this guide, and it is the source of truth for both:

- Rules a script can decide are checked by a validator, in a repository that has one (default-workspace-template's `validate_skill.py`).
- Rules that need judgement are reviewed against this guide by the `skill_hygiene` category in `.reviewer/code-issue-categories.md`.

## Contents

- Why size and structure matter
- SKILL.md
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

The second is compaction, which decides how much survives a long task. When Claude Code summarizes a conversation it re-attaches only the first 5,000 tokens of each invoked skill, 25,000 tokens across all of them, most recently invoked first, and it never re-reads the file. A reference the agent merely read is not re-attached at all. Other harnesses differ, and this is the tightest budget we know of, so skills are written to it.

Measured over 111 skill markdown files, this prose is 2.8 bytes per token on the current Claude tokenizer, with a 5% spread. Words and lines are poor proxies. So:

- 5,000 tokens is 14 KB, about 250 lines.
- A 500-line SKILL.md is about 10,000 tokens: its second half is gone after one compaction.

## SKILL.md

SKILL.md is the checklist an agent works from, not the manual.

- **Size.** At most 14,000 bytes for the whole file, and at most 500 lines of body.
- **Order.** Standing instructions first, then the step list, then the detail of each step. What comes first is what survives.
- **Standing instructions.** The rules that hold for the whole task ("never merge the worker's branch"), stated once, as rules and not as steps.
- **Step list.** Numbered, one line per step, copyable as a checklist. Mark each step that ends the turn. A SKILL.md over 200 lines must have it, or a contents list, within its first 100 lines, naming every H2 section below it.
- **Each step names its reference.** A step that needs more than a few lines says which file to read and when ("if the push is rejected, read `references/failures.md`"). An agent that has been compacted re-reads what its current step names, so a step with no pointer is a step with no detail.
- **Move out** anything most runs do not need: rare paths, lookup tables, long examples, flag references, troubleshooting. Keep in what every run needs.
- **One term per concept**, and one default with an escape hatch in place of a menu of options.

## References

A reference is any file a skill points an agent at: its own `references/` files, and files shared between skills.

- **A link is a path.** Naming a file by a path that resolves links to it: from the repository root, from the linking file's own directory or its `references/`, or, in one of the skill's own files, from the skill's directory or its `references/`. A path into a copy of the tree kept elsewhere in the repository (`<copy>/.agents/...`) links the file it copies. A filename pattern such as `op-<operation>.md` links to nothing, so list the concrete files where the choice is made.
- **Depth of two.** Every file a skill reaches is at most two links from its SKILL.md: SKILL.md links a reference, and that reference may link one file further. When a reference needs a third file, name that file from SKILL.md or from the first reference instead. To mention a file without sending the reader to it, describe its role and leave out its path.
- **Reachability.** Every file in a skill's directory is reached from its SKILL.md, and every shared file is reached from some skill, from the repository's instructions file, or from the program that loads it. Delete a file nothing reaches.
- **Link with a condition.** Say when to read the file, not only that it exists.
- **Contents list.** A reference over 100 lines must have, within its first 100 lines, a list naming every H2 section below it, so an agent can find one section without reading the file.
- **A long task's spine says so.** A reference an agent follows end to end tells it to re-read the file after a compaction.
- **Shared references stay shared.** They exist so one fact has one home. When one grows large, split it into a core that keeps its path and rare-path siblings the core links conditionally. Never copy its content into the skills that use it.

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

- It lives in one reference per skill, never interleaved with the live path. The live path carries at most a one-line conditional pointer to it.
- Each item carries a `CLEANUP:` marker saying what can be removed and when.
- The live path has no dates and no "currently", "new" or "not yet".

## Paths

- A path inside this repository must exist.
- A path that sends the reader to a file in this repository is written relative, to the repository root or to the file that names it. An absolute path (`/home/user/workspace/...`, `~/...`) links nothing: a file named only that way counts as unreached, and nothing checks that the path exists. Keep absolute paths for a command that has to run from another directory.
- A path that is not in this tree is written with a placeholder root: `<mngr>/apps/minds/README.md` for another repository, `<template>/.agents/skills/welcome/SKILL.md` for a repository being adopted.
- An example name is a placeholder too: `system/apps/<name>/app.toml`, not `system/apps/foo/app.toml`.
- Absolute paths, globs, and paths git ignores (generated files, a workspace's data) are not checked.

## Exemptions

An exemption records that a skill fails named checks and may, for now. It lives in the skill's own frontmatter, as two `metadata` strings:

```yaml
metadata:
  exempt-checks: size depth
  exempt-reason: awaiting-restructure
```

The checks are `size`, `reachability`, `depth`, `paths` and `contents`. There are two reasons:

| Reason | Meaning | Who writes it | Who clears it |
|---|---|---|---|
| `predates-checks` | The skill existed before the checks did. | Only a flow that brings existing skills under the checks: an update, a migration, adopting a template. Never by hand. | Whoever next edits the skill. |
| `awaiting-restructure` | A built-in skill whose restructure is scheduled. | Only the maintainers of the repository that ships the skill. | The same maintainers, in the release that restructures it. |

The rules around them:

- A failing check is fixed by restructuring the skill: splitting, moving and rewriting its files is always permitted. An exemption is never the answer to a failure.
- A new skill carries no exemption.
- An edit to a skill that carries `predates-checks` clears it: fix what the exempted checks report, then remove the two lines.
- An edit to a skill that carries `awaiting-restructure` leaves the exemption, and the skill's structure, as they are. Restructuring it in a copy the repository does not own only creates a divergence the real restructure has to reconcile. The edit must not fail any further check.
- An exemption naming a check the skill now passes is stale, and stale is an error.
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
| step list | The numbered list of a skill's steps near the top of its SKILL.md. |
| reference | A file a skill points an agent at, read on demand. |
| shared reference | A reference outside any one skill's directory, used by several. |
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
