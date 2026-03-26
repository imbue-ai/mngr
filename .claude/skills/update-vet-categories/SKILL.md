---
name: update-vet-categories
description: Update vet issue category overrides after editing the category .md files directly. Use when you've changed code-issue-categories.md or conversation-issue-categories.md and need to sync the override script.
---

# Updating Vet Issue Categories

This skill enables editing the issue category `.md` files directly and then updating `scripts/verify_skill_overrides.py` to match, so the generator stays consistent with your edits.

## Background

The issue category files in `.claude/agents/categories/` are generated from vet (an external repo) plus mng-specific overrides defined in `scripts/verify_skill_overrides.py`. The generator script `scripts/generate_verify_skills.py` reads vet's base categories, applies the overrides, and writes the final `.md` files.

The workflow is: edit the `.md` files to say what you want, then update the override script so the generator reproduces your edits.

Override actions available:
- `APPEND_GUIDE` / `APPEND_EXAMPLES` / `APPEND_EXCEPTIONS` -- add content after vet's base
- `REPLACE_GUIDE` / `REPLACE_EXAMPLES` / `REPLACE_EXCEPTIONS` -- completely replace vet's base content for that field (use when you want full control over the output regardless of what vet provides)
- `ADD_CATEGORY` -- add an entirely new category (via `NEW_CATEGORIES` dict)

## Safety Checks

Before doing any work:

1. **Ensure the working tree is clean (aside from the category file edits).** Run `git status` and confirm there are no other uncommitted changes. The override script must always be updated from a known-good committed state so that changes can be reviewed and reverted cleanly.
2. **Ensure VET_REPO is set.** The generator requires a vet checkout. Run `echo $VET_REPO` to confirm. If not set, ask the user for the path.

## Instructions

### 1. Save aside the desired category file content

The user has edited `.claude/agents/categories/code-issue-categories.md` (or `conversation-issue-categories.md`) to contain the content they want. Save a copy or note the diff:

```bash
git diff .claude/agents/categories/
```

This diff is what you need the overrides to produce.

### 2. Understand the current overrides

Read `scripts/verify_skill_overrides.py` to understand the existing overrides. For each category section that changed in the diff, determine whether it is currently handled by an APPEND or REPLACE override (or has no override at all).

### 3. Update the override script to produce the desired output

Edit `scripts/verify_skill_overrides.py` to make the generator output match the edited `.md` file:

- **If a category section's guide/examples/exceptions were changed**: use `REPLACE_GUIDE`, `REPLACE_EXAMPLES`, or `REPLACE_EXCEPTIONS` to set the complete desired content. REPLACE overrides completely discard whatever vet provides for that field, so include ALL desired content (not just the delta).
- **If an existing APPEND override needs to become a REPLACE**: remove the APPEND entry and add a REPLACE entry with the full desired content.
- **If a new category needs to be added**: add it to `NEW_CATEGORIES` with the guide text and the `insert_after` anchor.
- **If content was only appended** (not replacing vet's base): use `APPEND_*` as before.
- Keep overrides organized by the order categories appear in the output file.

### 4. Regenerate and verify

Regenerate the category files from vet + updated overrides:

```bash
uv run python scripts/generate_verify_skills.py
```

Then verify the output matches what was desired:

```bash
uv run python scripts/generate_verify_skills.py --check
git diff .claude/agents/categories/
```

The `--check` must pass, and the diff against the original edits should be empty (or only contain intended adjustments). If the generated output doesn't match, iterate on the overrides.

### 5. Commit

Commit both the override script changes and the regenerated category files together in a single commit.
