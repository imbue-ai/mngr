---
argument-hint: <script_file> <test_directory>
description: Match tutorial script blocks to pytest functions and add missing tests
---

Your task is to ensure that every command block in a tutorial shell script has a corresponding pytest function.

## Step 1: Run the matcher

Run the tutorial matcher script to find unmatched blocks and functions:

```bash
uv run python scripts/tutorial_matcher.py $ARGUMENTS
```

If the output says everything is matched, you are done.

## Step 2: Understand the context

Read the tutorial script file and the test directory to understand the overall structure and conventions used in the existing tests.

Pay close attention to:
- How existing test functions are structured (fixtures, assertions, setup/teardown)
- What the tutorial script is demonstrating (the commands, their arguments, expected behavior)
- The patterns used for docstrings (the block must appear verbatim in the docstring)

## Step 3: Add missing pytest functions

For each unmatched script block reported by the matcher:

1. Understand what the block does by reading the surrounding context in the tutorial script.
2. Write a pytest function that tests the behavior demonstrated by that block.
3. The function's docstring MUST contain the exact text of the script block (indented to match Python syntax). The docstring may contain additional content beyond the block.
4. The function name should be descriptive of what the block does (e.g., `test_create_task` for a block that runs `mng create ...`).
5. Follow the existing test patterns in the directory for style, fixtures, and assertions.

## Step 4: Handle unmatched pytest functions

For each pytest function that doesn't correspond to any script block:

- If the block was removed from the script, consider whether the test is still useful. If not, remove it.
- If the docstring has a typo or doesn't exactly match the block, fix the docstring.

## Step 5: Verify

Re-run the matcher to confirm everything is matched:

```bash
uv run python scripts/tutorial_matcher.py $ARGUMENTS
```

Then run the tests in the test directory to make sure all tests pass.
