---
argument-hint: <script_file> <test_directory>
description: Match tutorial script blocks to e2e pytest functions and add missing tests
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

## Step 3: Handle unmatched pytest functions

Handle these FIRST, before adding new tests, because some of these may pair up with unmatched script blocks.

For each pytest function that doesn't correspond to any script block, compare its docstring against the list of unmatched script blocks. If there is a script block that mostly matches (e.g., a command was renamed, a flag was added, or a line was changed), the script block was likely modified after the test was written. In that case, update the test's docstring to exactly reproduce the current script block, and update the test logic to match the new behavior. This also resolves that script block, so it no longer needs a new test in step 4.

If no script block is even a close match, the block was removed from the script entirely. Remove the test function.

## Step 4: Add tests for remaining unmatched script blocks

After step 3, some script blocks may still lack tests. For each one:

1. Understand what the block does by reading the surrounding context in the tutorial script.
2. Write one or more pytest functions that test the behavior demonstrated by that block. A single block may warrant multiple tests if it demonstrates multiple behaviors or has interesting edge cases.
3. Each function's docstring MUST contain the exact text of the script block (indented to match Python syntax). The docstring may contain additional content beyond the block.
4. The function name should be descriptive of what the block does (e.g., `test_create_task` for a block that runs `mng create ...`).
5. Decorate each new test function with `@pytest.mark.release`, since these are e2e tests.
6. Follow the existing test patterns in the directory for style, fixtures, and assertions.

## Guidelines for writing test logic

When writing or updating tests, follow these two principles:

**Run the actual commands from the script block.** The test must run commands that match the script block as closely as possible. The only acceptable differences are adding flags like `--no-connect` to make commands work without an interactive terminal. For example, if the script block demonstrates `mng create --foo`, the test must run `mng create --foo` (with optional extra flags) -- it must NOT simply run `mng create --help` and verify that `--foo` is a supported flag.

**Verify the actual behavior, not just surface-level output.** The script blocks usually don't contain verification code, but the test must verify the exact desired behavior as thoroughly as possible. For example, if a script block creates an agent in a specific directory, it is not sufficient to only verify that the agent appears in the result of `mng list` -- you must also verify that the agent is running in that directory, e.g. by running `mng exec $agent_name pwd` and checking its output. Think about what the command is supposed to accomplish and assert on the concrete effects.

## Step 5: Verify

Re-run the matcher to confirm everything is matched:

```bash
uv run python scripts/tutorial_matcher.py $ARGUMENTS
```

Do NOT run the tests locally -- these are e2e tests and may be too expensive to run locally. They will be validated in CI.
