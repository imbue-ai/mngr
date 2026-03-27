---
name: writing-ratchet-tests
description: Write ratchet tests to prevent accumulation of code anti-patterns. Use when asked to create a "ratchet test" for tracking and preventing specific code patterns (e.g., TODO comments, inline imports, broad exception handling).
---

# Writing Ratchet Tests

This skill provides guidelines for writing ratchet tests that prevent accumulation of code anti-patterns in a project.

## What are Ratchet Tests?

Ratchet tests are a testing pattern that:
- Track the current count of a specific anti-pattern in the codebase
- Prevent that count from increasing (using inline-snapshot)
- Allow the count to decrease (improvement is always allowed)
- Provide clear, actionable feedback when violations increase

Common use cases:
- TODO comments
- Inline imports
- Use of eval() or exec()
- Broad exception handling (bare except, except Exception)
- Any other code pattern you want to gradually eliminate

## Instructions

When asked to create a ratchet test, follow these steps:

### 1. Understand the Pattern

First, clarify what pattern needs to be tracked by answering these questions for yourself:
- What specific code pattern should be prevented?
- Why is this pattern problematic?
- What regex or detection method will identify it?
- Does the regex need multiline support?

### 2. Locate or Create test_ratchets.py

Check if the project has a `test_ratchets.py` file in the source package (e.g., `libs/<project>/imbue/<project>/utils/test_ratchets.py`):
- If it exists, add the new test to the existing file
- If it doesn't exist, create it in an appropriate location within the source package (commonly in a `utils/` folder)

The file should import:

```python
from pathlib import Path

from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet
from imbue.imbue_common.ratchet_testing.core import format_ratchet_failure_message
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
```

### 3. Create Helper Function (if needed)

If this is the first ratchet test in the file, create a helper to get the source directory:
```python
def _get_<project>_source_dir() -> Path:
    return Path(__file__).parent.parent
```

This assumes the test file is in a subfolder (like `utils/`) of the main source package. Adjust the path navigation as needed based on where your test file is located. Replace `<project>` with the actual project name (e.g., "mngr").

### 4. Write the Test Function

Create a test function following this pattern:
```python
def test_prevent_<pattern_name>() -> None:
    pattern = RegexPattern(r"<regex_pattern>", multiline=<True|False>)
    chunks = check_regex_ratchet(_get_<project>_source_dir(), FileExtension(".py"), pattern)

    assert len(chunks) <= snapshot(), format_ratchet_failure_message(
        rule_name="<pattern name>",
        rule_description="<why this pattern is problematic>",
        chunks=chunks,
    )
```

Key points:
- Function name: `test_prevent_<descriptive_name>`
- No docstring needed (keep it concise)
- Use `multiline=True` if the regex needs to match at line starts (uses `^` anchor)
- Leave `snapshot()` empty initially - it will be filled when the test runs
- Provide clear, actionable rule_name and rule_description

### 5. Run the Test to Create Snapshot

Run the test with inline-snapshot create mode (use the actual path to your test file):
```bash
uv run pytest libs/<project>/imbue/<project>/utils/test_ratchets.py::test_prevent_<name> --inline-snapshot=create -v
```

This will:
- Find all current violations in the codebase
- Create a snapshot with the current count
- Establish the ratchet baseline

### 6. Verify the Test

Run the test normally to ensure it passes:
```bash
uv run pytest libs/<project>/imbue/<project>/utils/test_ratchets.py -v
```

## Best Practices

- Keep ratchet test functions concise with no docstrings
- Use clear, descriptive test names: `test_prevent_<anti_pattern>`
- Provide helpful rule descriptions that explain WHY the pattern is problematic
- Start with `snapshot()` empty - let the test fill it in
- Use `multiline=True` when your regex uses `^` to match line starts
- Group related ratchets in the same file
- Run all ratchet tests together: `uv run pytest libs/<project>/imbue/<project>/utils/test_ratchets.py -v`

## Troubleshooting

**Pattern not matching expected violations:**
- Check if you need `multiline=True` for patterns using `^` or `$`
- Verify the regex is correct using a regex tester
- Check that the file extension is correct
- Ensure violations are in git-tracked files (git blame only works on committed code)

**Test fails after running:**
- This is expected if the current count is higher than the snapshot
- Always fix the violations if a ratchet test fails--it's because you messed something up. NEVER run with `--inline-snapshot=fix`
- Never just blindly update snapshots - investigate why the count increased

**Snapshot shows 0 but violations exist:**
- The regex pattern might be incorrect
- Try running with `multiline=True` if using `^` or `$` anchors
