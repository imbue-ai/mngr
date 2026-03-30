---
name: verify-and-fix
description: Verify the current branch for code issues, plan fixes, and implement them.
---

You are an autonomous code verifier and fixer. You will verify the current branch for issues, plan fixes, and implement them. Do not ask any questions. Use your best judgment throughout.

# Step 1: Gather Context

First, understand what you're working with.

1. Get the diff of changes on this branch (between the current code and the base branch):

```bash
git diff <base_branch>...HEAD
```

2. Read any relevant instruction files (CLAUDE.md, style_guide.md) that apply to the changed code.
3. Understand the existing codebase patterns around the changed files.
4. Read the issue categories file whose path you were given.

If the diff is empty (no changes on the branch), create the verification marker by running `date -u +%Y-%m-%dT%H:%M:%SZ > .reviewer/outputs/autofix/verified.md` then stop immediately -- there is nothing to verify or fix.

# Step 2: Create Issue List

Go through the diff and create a comprehensive list of ALL potential issues you notice. Be thorough -- it's better to identify more potential issues initially than to miss something.

For each potential issue, note:
- The issue type (from the categories file)
- The specific location (file and line number)
- A brief description of what you observed

Then, for each potential issue, briefly check: is this actually a problem, or does it fall under one of the listed exceptions for that issue type? Drop anything that clearly isn't a real issue. Keep everything else, regardless of severity.

If there are no issues, create the verification marker by running `date -u +%Y-%m-%dT%H:%M:%SZ > .reviewer/outputs/autofix/verified.md` then stop here. There is nothing to fix.

## Record Issues

After finalizing the issue list, use the Write tool (without checking if the directory exists) to write all issues to `.reviewer/outputs/autofix/issues/{hash}.jsonl` (where `{hash}` is the *full* HEAD hash you were given). Write one JSON object per line with these fields (in order):

- `issue_type`: the issue type code (e.g., "logic_error", "poor_naming")
- `file`: the file path
- `line`: the line number (or null if not applicable)
- `description`: a complete description of the problem
- `confidence`: a confidence score between 0.0 and 1.0 (probability it is an actual issue)
- `severity`: one of "CRITICAL", "MAJOR", "MINOR", or "NITPICK"

This file serves as a structured record of all identified issues, including those that may not be fixed. Do NOT commit this file -- it is gitignored.

# Step 3: Plan and Fix

For each issue, do the following in order:

## Planning phase (do this BEFORE writing any code)

1. Read the relevant source files thoroughly.
2. Understand the surrounding code, architecture, and any related abstractions.
3. Determine the correct fix.
4. Get the *full* current HEAD hash: `git rev-parse HEAD`. Use the Write tool, without checking if the directory exists, to create `.reviewer/outputs/autofix/plans/<hash>_<issue_number>.md` describing:
   - What the issue is and where it is
   - Why it is a problem
   - The planned fix (specific changes to specific files)
   - Any risks or edge cases to watch for

## Implementation phase

5. Implement the fix according to your plan.
6. Commit only the code changes. Do NOT use `git add -f` (files in `.reviewer/` are gitignored and must stay that way). Use this format:

```
<short summary>

Problem: <what the issue was and where>
Fix: <what was changed and why>
```

Repeat for each issue. Each fix MUST be its own separate commit.

# Step 4: Post-fix Validation

After all fixes are committed, run the project test suite. Use whatever test command is specified in the project's CLAUDE.md or README. If none is specified, try `uv run pytest` or the most obvious equivalent.

If tests pass, you are done.

If tests fail, fix the failures and commit the fixes. Re-run the tests. Keep fixing and re-running until tests pass. The only acceptable exception is if you can prove a failure is preexisting by running the same test on the base branch and seeing it fail there too.

Once tests pass, create the verification marker by running `date -u +%Y-%m-%dT%H:%M:%SZ > .reviewer/outputs/autofix/verified.md`

