---
name: sync-tutorial-to-e2e-tests
argument-hint: <script_file> <test_directory>
description: Match tutorial script blocks to e2e pytest functions and add missing tests
---

Default arguments (if none provided): `libs/mngr/imbue/mngr/resources/mega_tutorial.sh libs/mngr/imbue/mngr/e2e/tutorial`

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
- The docstring format (below): a verbatim `Tutorial block:` section followed by a `Scope:` section. The matcher reads the block from the `Tutorial block:` section.

### The docstring format

Each tutorial test's docstring is **strictly** the verbatim tutorial block plus its scope -- no leading summary line:

```python
@pytest.mark.release
def test_foo(e2e: E2eSession, agent_name: str) -> None:
    """Tutorial block:
        # comment from tutorial
        mngr create my-task --some-flag
        # another comment

    Scope: <crystallized scope -- see step 4>.
    """
    result = e2e.run("mngr create my-task --some-flag")
    assert result.exit_code == 0
```

- The `Tutorial block:` section holds the script block **verbatim** (the matcher checks this). It is dedented naturally with the surrounding Python; indent the block lines under the header.
- The `Scope:` section is the crystallized scope you author in step 4.

## Step 3: Handle unmatched pytest functions

Handle these FIRST, before adding new tests, because some of these may pair up with unmatched script blocks.

For each pytest function that doesn't correspond to any script block, compare its docstring's `Tutorial block:` section against the list of unmatched script blocks. If there is a script block that mostly matches (e.g., a command was renamed, a flag was added, or a line was changed), the script block was likely modified after the test was written. In that case, update the `Tutorial block:` section to exactly reproduce the current script block, update the test logic to match the new behavior, and update the `Scope:` accordingly. This also resolves that script block, so it no longer needs a new test in step 4.

If no script block is even a close match, the block was removed from the script entirely. Remove the test function.

## Step 4: Add tests for remaining unmatched script blocks

After step 3, some script blocks may still lack tests. Add tests to the appropriate existing test file, or create a new file if the blocks belong to a distinct section (e.g., `test_create_remote.py` for "CREATING AGENTS REMOTELY" blocks).

### Requirements for each test function

- Docstring in the format above: the verbatim `Tutorial block:` section, then the `Scope:` section.
- Decorate with `@pytest.mark.release`.
- Use `e2e: E2eSession` as the fixture type.
- Run the actual command from the block (not just `--help`).
- Assertions that verify the scope (see below).
- Follow existing patterns in the directory for style and fixtures.

### Crystallizing the scope

The `Scope:` is the contract TMR enforces: it states exactly what the test must verify, so TMR never has to re-derive it from the commands. The tutorial block is the lower bound on scope. Crystallize the *implicit* requirements of the block's commands into the `Scope:` prose, and write assertions that match it:

- **Command level**: running `mngr foo` must do *something* observable -- a real-world side effect or something in its output. State the effect, and assert it the way a human would when debugging (files, git status, command output). Golden rule: the assertion should pass for this command, but FAIL if the command were not run or were replaced by a no-op. E.g. if the block creates an agent in a directory, the scope says to verify it is running there (`mngr exec $agent pwd`), not merely that the command exited 0.
- **Flag level**: `mngr foo --bar=lorem` must do something *different* from the same command without `--bar=lorem`. State and verify that difference.
- **Stay within the block**: do not invent scope the block does not demonstrate. A scope item (and its assertion) must trace back to a command or flag in the block. Occasionally the right scope is the *absence* of an effect (establish a baseline, check it has not moved) -- include that only when the block implies it.

Keep the scope tight: enough to prove the documented behavior, no gold-plating.

## Step 5: Verify

Re-run the matcher to confirm everything is matched:

```bash
uv run python scripts/tutorial_matcher.py $ARGUMENTS
```

Do NOT run the tests locally -- these are e2e tests and may be too expensive to run locally. They will be validated in CI.
