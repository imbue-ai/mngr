---
name: sync-tutorial-to-e2e-tests
argument-hint: <script_file> <test_directory>
description: Match tutorial script blocks to e2e pytest functions and add missing tests
---

Default arguments (if none provided): `libs/mngr/tutorials/mega_tutorial.sh libs/mngr/imbue/mngr/e2e`

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
- The patterns used for `write_tutorial_block()` calls (the block must appear verbatim in the call argument)

## Step 3: Handle unmatched pytest functions

Handle these FIRST, before adding new tests, because some of these may pair up with unmatched script blocks.

For each pytest function that doesn't correspond to any script block, compare its `write_tutorial_block()` call against the list of unmatched script blocks. If there is a script block that mostly matches (e.g., a command was renamed, a flag was added, or a line was changed), the script block was likely modified after the test was written. In that case, update the `write_tutorial_block()` argument to exactly reproduce the current script block, and update the test logic to match the new behavior. This also resolves that script block, so it no longer needs a new test in step 4.

If no script block is even a close match, the block was removed from the script entirely. Remove the test function.

## Step 4: Add tests for remaining unmatched script blocks

After step 3, some script blocks may still lack tests.

The priority is coverage, not perfection -- a separate step will improve test quality later. What matters is that each tutorial block has a corresponding test function with the correct `write_tutorial_block()` call and at least a basic assertion.

Add tests to the appropriate existing test file, or create a new file if the blocks belong to a distinct section (e.g., `test_create_remote.py` for "CREATING AGENTS REMOTELY" blocks).

### Requirements for each test function

Each function MUST call `e2e.write_tutorial_block("""...""")` as its first statement, with the **exact** text of the script block (the matcher checks this). The block text will be dedented and stripped automatically, so indent it naturally with the surrounding Python code. Example:
```python
@pytest.mark.release
def test_foo(e2e: E2eSession, agent_name: str) -> None:
    e2e.write_tutorial_block("""
        # comment from tutorial
        mngr create my-task --some-flag
        # another comment
    """)
    result = e2e.run("mngr create my-task --some-flag")
    assert result.exit_code == 0
```

Other requirements:
- Decorate with `@pytest.mark.release`
- Use `e2e: E2eSession` as the fixture type
- Run the actual command from the block (not just `--help`)
- At least one basic assertion (exit code check is fine)
- Follow existing patterns in the directory for style and fixtures

## Step 5: Verify

Re-run the matcher to confirm everything is matched:

```bash
uv run python scripts/tutorial_matcher.py $ARGUMENTS
```

Do NOT run the tests locally -- these are e2e tests and may be too expensive to run locally. They will be validated in CI.
