IT IS CRITICAL TO FOLLOW ALL INSTRUCTIONS IN THIS FILE DURING YOUR WORK ON THIS PROJECT.

IF YOU FAIL TO FOLLOW ONE, YOU MUST EXPLICITLY CALL THAT OUT IN YOUR RESPONSE.

# Important things to know:

- This is a monorepo.
- ALWAYS run commands by calling "uv run" from the root of the git checkout (ex: "uv run mng create ..."). Do NOT call "mng" directly (it will refer to the wrong version).
- NEVER amend commits or rebase--always create new commits.
- If you ever need to work with another *git* repo that is *outside* of this monorepo, you should do so by creating a worktree for that repo and putting it within a ".external_worktrees/" directory *within* this root folder (ie, at the same level as the ".git/" folder).  Use the same branch name in the worktree as the branch you are working on in this repo, and be sure to commit inside there as well.

# How to get started on any task:

Always begin your session by reading the relevant READMEs and any other related documentation in the docs/ directory of the project(s) you are working on. These represent *user-facing* documentation and are the most important to understand.

Once you've read these once during a session, there's no need to re-read them unless explicitly instructed to do so.

If you will be writing code, be sure to read the style_guide.md for the project. Then read all README.md files in the relevant project directories, as well as all `.py` files at the root of the project you are working on (ex: `primitives.py`, etc.). Also read everything in data_types, interfaces, and utils to ensure you understand the core abstractions.

Then take a look at the other code directories, and based on the task, determine which files are most relevant to read in depth. Be sure to read the full contents from those files.

Do NOT read files that end with "_test.py" during this first pass as they contain unit tests (unless you are explicitly instructed to read the unit tests).

Do NOT read files that start with "test_" either, as they contain integration, acceptance, and release tests (again, unless you are explicitly instructed to read the existing tests).

Only after doing all of the above should you begin writing code.

# Important commands and conventions:

- Never run `uv sync`, always run `uv sync --all-packages` instead

# Always remember these guidelines:

- Never misrepresent your progress. It is far better to say "I made some progress but didn't finish" than to say "I finished" when you did not.
- Always finish your response by reflecting on your work and identify any potential issues.
- If I ask for something that seems misguided, flag that immediately. Then attempt to do whatever makes the most sense given the request, and in your final reflection, be sure to flag that you had to diverge from the request and explain why.
- During your final reflection, if you see a potentially better way to do something (e.g. by using an existing library or reusing existing code), flag that as a potential task for future improvement.
- Never use emojis. Remove any emojis you see in the code or docs whenever you are modifying that code or those docs.
- Be concise in your communications. Don't hype up your results, say "perfect!", or use emojis. Be serious and professional.

# When coding, follow these guidelines:

- Only make the changes that are necessary for the current task.
- Before implementing something, check if there is something in the codebase or look for a library
- Reuse code and use external dependencies heavily. Before implementing something, make sure that it doesn't already exist in the codebase, and consider if there's a library that can be imported instead of implementing it yourself. We want to be able to maintain the minimum amount of code that gets the job done, even if that means introducing dependencies. If you don't know of a library but think one might be plausible, search the web. (I'm even open to using random GitHub projects, but run anything that's not a well-established library by me first so I can check if it's likely to be reliable.)
- Code quality is extremely important. Do not compromise on quality to deliver a result--if you don't know a good way to do something, ask.
- Follow the style guide!
- Use the power of the type system to constrain your code and provide some assurance of correctness. If some required property can't be guaranteed by the type system, it should be runtime checked (i.e. explode if it fails).
- Avoid using the `TYPE_CHECKING` guard. Do not add it to files that do not already contain it, and never put imports inside of it yourself--you MUST ask for explicit permission to do this (it's generally a sign of bad architecture that should be fixed some other way).
- Do NOT write code in `__init__.py`--leave them completely blank (the only exception is for a line like "hookimpl = pluggy.HookimplMarker("mng")", which should go at the very root __init__.py of a library).
- Do NOT make constructs like module-level usage of `__all__`
- Before finishing your response, if you have made any changes, then you must ensure that you have run ALL tests in the project(s) you modified, and that they all pass. DO NOT just run a subset of the tests! However, while iterating (e.g. fixing a failing test, developing a feature), run only the relevant tests for rapid feedback -- save the full suite for the final check.
- To run tests for a single project: "cd libs/mng && uv run pytest" or "cd apps/minds && uv run pytest". Each project has its own pytest and coverage configuration in its pyproject.toml.
- While you're iterating, you can pass "--no-cov --cov-fail-under=0" to disable coverge (slightly faster), but during your final check, you *MUST NOT* pass those flags (it will fail in CI anyway)
- For faster iteration, add "-m 'not tmux and not modal and not docker and not docker_sdk and not acceptance and not release'" to skip slow infrastructure tests (~30s instead of ~95s). These still run in CI. Note that you *MUST* also pass "--no-cov --cov-fail-under=0" when doing this, otherwise it will complain about a lack of coverage.
- Running pytest will produce files in .test_output/ (relative to the directory you ran from) for things like slow tests and coverage reports.
- Note that "uv run pytest" defaults to running all "unit" and "integration" tests, but the "acceptance" tests also run in CI. Do *not* run *all* the acceptance tests locally to validate changes--just allow CI to run them automatically after you finish responding (it's faster than running them locally).
- If you need to run a specific acceptance or release test to write or fix it, iterate on that specific test locally by calling "just test <full_path>::<test_name>" from the root of the git checkout. Do this rather than re-running all tests in CI.
- Note that tasks are *not* allowed to finish without A) all tests passing in CI, B) running /autofix to verify and fix code issues, and C) running /verify-conversation to review the conversation for behavioral issues.
- A PR will be made automatically for you when you finish your reply--do NOT create one yourself.
- To help verify that you ran the tests, report the exact command you used to run the tests, as well as the total number of tests that passed and failed (and the number that failed had better be 0).
- If tests fail because of a lack of coverage, you should add tests for the new code that you wrote.
- When adding tests, consider whether it should be a unit test (in a _test.py file) or an integration/acceptance/release test (in a test_*.py file, and marked with @pytest.mark.acceptance or @pytest.mark.release, no marks needed for integration).  See the style_guide.md for exact details on the types of tests. In general, most slow tests of all functionality should be release tests, and only important / core functionality should be acceptance tests.
- Use the shared fixtures (`temp_host_dir`, `temp_mng_ctx`, `local_provider`, etc.) instead of creating your own.
- Do NOT create tests for test utilities (e.g. never create `testing_test.py`). Code in `testing.py` and `conftest.py` is exercised by the tests that use it and does not need its own test file.
- Do NOT create tests that code raises NotImplementedError.
- If you see a flaky test, YOU MUST HIGHLIGHT THIS IN YOUR RESPONSE. Flaky tests must be fixed as soon as possible. Ideally you should finish your task, then if you are allowed to commit, commit, and try to fix the flaky test in a separate commit.
- Do not add TODO or FIXME unless explicitly asked to do so
- Code must work on both macOS and Linux. It's ok if it doesn't work on Windows.
- To reiterate: code correctness and quality is the most important concern when writing code.

# Ratchets

Each project has a `test_ratchets.py` file containing automated code quality checks ("ratchets"). Each ratchet tracks a count of violations for a specific anti-pattern (e.g. raising built-in exceptions, using monkeypatch.setattr). The count can only stay the same or decrease -- increasing it fails the test.

Ratchets are guidance and reminders about good code, not rules to be blindly obeyed. When a ratchet fires on your code:

1. Understand *why* the ratchet exists by reading its `rule_description`. It explains the principle behind the check.
2. Fix the code in the spirit of the ratchet. For example, if `PREVENT_MONKEYPATCH_SETATTR` fires, a valid fix could be to use dependency injection -- not to manually save/restore the attribute with `try/finally`, which evades the regex while violating the same principle.
3. Never evade a ratchet. Restructuring code to dodge the regex pattern while still doing the same bad thing is worse than the original violation, because it hides the problem. Common evasion patterns include splitting a statement across lines, assigning to a temporary variable before the flagged operation, or using a synonym that the regex doesn't catch.
4. If you cannot find a fix that honors the spirit of the ratchet, **flag this to the user** rather than silently working around it. Do not use type-system escape hatches (e.g. assigning through `Any`, intermediate variables, or synonyms) to bypass a ratchet -- these are evasions even if they dodge the regex.
5. If the ratchet is a **true misfire** -- the regex pattern matched something that is genuinely not the anti-pattern it was designed to catch (e.g. a variable name that happens to contain a flagged substring, or a string literal / comment that matches the pattern) -- then first try to update the ratchet's regex to be more specific so it no longer misfires (be extra careful not to exclude any real violations in the process). If that's not feasible, bump the ratchet count and explain the misfire to the user. This is distinct from a case where there *is* a real violation but you believe it's "justified"; justified violations are still violations and should be handled per steps 1-4 above.

## Test fixture discovery

Before writing new tests, read the relevant `conftest.py` and `testing.py` files to avoid reimplementing things that already exist. Test infrastructure lives in these files:

| File pattern | Purpose |
|---|---|
| `conftest.py` | Pytest fixtures and hooks, scoped to the directory they're in (auto-discovered by pytest) |
| `testing.py` | Non-fixture test utilities: factory functions, helpers, context managers (explicitly imported) |
| `mock_*_test.py` | Concrete mock implementations of interfaces (explicitly imported) |

All fixtures must be in conftest.py, not in individual test files.

# Manual verification and testing

Before declaring any feature complete, manually verify it: exercise the feature exactly as a real user would, with real inputs, and critically evaluate whether it *actually does the right thing*. Do not confuse "no errors" with "correct behavior" -- a command that exits 0 but produces wrong output is not working.

Then crystallize the verified behavior into formal tests. Assert on things that are true if and only if the feature worked correctly -- this ensures tests are both reliable and meaningful.

## Verifying interactive components with tmux

For interactive components (TUIs, interactive prompts, etc.), use `tmux send-keys` and `tmux capture-pane` to manually verify them. This is a special case: do NOT crystallize these into pytest tests. They are inherently flaky due to timing and useless in CI, but valuable for agents to verify that interactive behavior looks right during development.

# Git and committing

If desired, the user will explicitly instruct you not to commit.

By default, or if instructed to commit:
- Commit frequently and in general use git operations to your advantage (e.g. by reverting commits rather than manually undoing changes).
- Commit with a sensible commit message when you finish your response.
- Be sure to add any files you made before committing. This includes specs (.md files), configs, etc.!
- Even when writing specs, you should commit (if not explicitly instructed not to).

If instructed not to commit:
- do not commit anything! Simply leave the git state as it is at the end of your response.
- NEVER run git commands like git reset, git checkout, etc that might change the git state (when instructed not to commit you are collaborating with others in the same directory, so should not change other files or the git state).

# Silly error workarounds

If you get a failure in `test_no_type_errors` that seems spurious, try running `uv sync --all-packages` and then re-running the tests. If that doesn't work, the error is probably real, and should be fixed.

If you get a "ModuleNotFoundError" error for a 3rd-party dependency when running a command that is defined in this repo (like `mng`), then run "uv tool uninstall mng && uv tool install -e libs/mng" (for the relevant tool) to refresh the dependencies for that tool, and then try running the command again.

If you get a failure when trying to commit the first time, just try committing again (the pre-commit hook returns a non-zero exit code when ruff reformats files).
