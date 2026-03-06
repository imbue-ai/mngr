---
allowed-tools: Bash:*
description: Review code changes in a branch for errors, issues, and quality problems.
---

# Code Verification and Review Guide

This skill provides comprehensive guidance for reviewing code changes to identify errors, quality issues, and problems before they are merged.

## Instructions

When asked to verify or review code changes, follow these steps:

### 1. Gather Context

First, understand what you're reviewing.

This is the transcript of the user's conversation, which should help you understand their intent:

---

!`export MAIN_CLAUDE_SESSION_ID=$(cat .claude/sessionid) && ./scripts/print_user_session.sh`

---

(if the above is empty, run this yourself: `export MAIN_CLAUDE_SESSION_ID=$(cat .claude/sessionid) && ./scripts/print_user_session.sh` )

This is the diff of the changes that resulted (between the current code and the target, eg, where we want to merge it to, eg, the output of git diff !`echo $GIT_BASE_BRANCH`..HEAD):

---

!`git diff $GIT_BASE_BRANCH...HEAD`

---

(if the above is empty, run this yourself: `git diff $GIT_BASE_BRANCH...HEAD` )

Also be sure to:
- Understand the existing codebase patterns around the changed files
- Read any relevant instruction files (CLAUDE.md, style_guide.md) that might apply to the changed code

Consider the user's request--if they did not ask for any changes and no changes were made (ex: they just asked a question), you can exit immediately. Otherwise, proceed to the next step.

### 2. Create Initial Issue List

Go through the diff and create a comprehensive list of ALL potential issues you notice. Be thorough at this stage--it's better to identify more potential issues initially than to miss something.

For each potential issue, note:
- The issue type (from the categories below)
- The specific location (file and line number)
- A brief description of what you observed

Put these observations into the "initial issues file" for tracking:

    .reviews/initial_issue_list/!`tmux display-message -t "$TMUX_PANE" -p '#W' || echo reviewer_0`.md

!`rm -rf .reviews/initial_issue_list/$(tmux display-message -t "$TMUX_PANE" -p '#W' || echo reviewer_0).md`

### 3. Analyze Each Potential Issue

After creating the initial list in that file, read that file, and, for each issue in that initial list:

1. think carefully about each of the following:
- Is this actually a problem, or is it acceptable given the context?
- Does it fall under any of the exceptions listed for that issue type?
- How severe is it? (CRITICAL / MAJOR / MINOR / NITPICK)
2. If it is a problem (that has not already been output), output the issue by appending to the "final output json file" with all the required details, in order (one JSON object per line, ie, one issue per line, see exact output format details below)

The "final output json file" is:

    .reviews/final_issue_json/!`tmux display-message -t "$TMUX_PANE" -p '#W' || echo reviewer_0`.json

!`rm -rf .reviews/final_issue_json/$(tmux display-message -t "$TMUX_PANE" -p '#W' || echo reviewer_0).json`

When finished with all issues, touch this file:

    .reviews/final_issue_json/!`tmux display-message -t "$TMUX_PANE" -p '#W' || echo reviewer_0`.json.done

!`rm -rf .reviews/final_issue_json/$(tmux display-message -t "$TMUX_PANE" -p '#W' || echo reviewer_0).json.done`

As you are doing your analysis, do *not* run the tests--just focus on the code review itself. CI will prevent failing tests.

---

## Issue Categories

Review the code for the following types of issues:

### commit_message_mismatch

The diff must completely fulfill the user's request.

**Look for incomplete implementations:**
- When the user asks for changes "throughout", "everywhere", or "all", verify ALL instances are modified
- If multiple changes are requested, ensure each one is fully implemented
- Check that fixes are applied to all occurrences of a pattern, not just some

**Look for scope mismatches:**
- Changes only in initialization when they should apply during execution
- Modifications to only one file when multiple files need updates
- Partial refactoring that leaves related code unchanged

**Look for unauthorized changes:**
- Configuration changes (linting, build, test settings) not requested
- New features or options beyond the request
- Changes to unrelated code

**Look for unintended removals:**
- Removal of project-specific configuration or settings that should be preserved
- Deletion of functionality that is still needed
- Loss of necessary entries when replacing configuration files

**Look for unintended side effects:**
- Changes that affect code paths or functionality beyond what was requested
- Modifications that impact how existing features work in ways not mentioned in the request

**Exceptions:** Minor refactors directly related to requested changes are acceptable.

---

### commit_contents

The diff should not include excessive changes, or changes unrelated to the user's request.  In particular, avoid:

1. Checking in binaries, compiled files, dependencies, or build artifacts
2. Accidental deletion of files or folders
3. Unrequested changes to test time limits, config vars, minimum required test coverage, ratchet test values, and any other settings that are supposed to constrain the codebase. It is *very* important to flag these issues as a MAJOR issue!

---

### documentation_implementation_mismatch

The implementation should follow, in this priority order:
1. The user's request
2. Documentation existing in the code base (e.g. the style guide)
3. Existing code around it
4. General best practices and common sense

If the user's request conflicts with the state of documentation in the code base, the documentation should be updated to reflect the new user request.

**Examples:**
- The docstring of a class or function does not match what the class or function implements
- The repository contains a README.md file with instructions that are not adhered to by the code
- The diff implements significant new functionality, but existing documentation within the repository is not updated
- Inline comments are not updated even though functionality was changed by the diff
- Documentation contains outdated code snippets or commands that need to be updated because of the changes made by the diff

**Exceptions:** TODOs and FIXMEs that are not implemented yet are not considered a documentation mismatch.

---

### incomplete_integration_with_existing_code

The diff should follow existing architectural and organizational patterns in the codebase:
- If the codebase uses a modular structure with separate files for classes/components, new classes should follow the same pattern
- If the codebase organizes code in specific directories (e.g., src/, components/, utils/), new code should be placed accordingly
- If the codebase uses specific import/export patterns (e.g., relative vs. absolute imports), new code should use the same patterns
- Tests should be given the correct decorators (ex: @pytest.mark.acceptance for tests that require network access/credentials and @pytest.mark.release for end-to-end tests that are not "core", eg, test rarer cases)
- Tests should be placed in the correctly named file (ex: *_test.py for unit tests, test_*.py for integration/acceptance/release tests)

The diff should integrate functionally with existing code by adding invocations, updating invocations, replacing code with newly defined functions or variables, removing duplicate code when a new piece replaces it, etc.

Prefer using existing library/dependency APIs over custom implementations when the library provides (or will provide) the needed functionality.

**Examples:**
- The codebase uses absolute Python imports from the project root, but a new file uses relative imports
- The codebase places classes into separate files under a source directory, but new classes are all added to an existing file
- The diff implements a new function, but doesn't add any callsites for it
- A new optional parameter is added to a function to implement a requested feature, but existing callsites are not updated to make use of this parameter
- A named constant is introduced to replace a hard-coded inline literal, but existing code is not updated to make use of the new constant everywhere
- Custom code is added to implement functionality that an existing external library already provides or will provide in newer versions

---

### user_request_artifacts_left_in_code

Comments should describe what the code does, not how it was changed.

**Flag:**
- Comments that reference the change process: "# Changed from X to Y", "# Updated to print less"
- Comments that mention fixing or addressing issues: "# Fixed bug where...", "# This addresses..."
- Documentation written in past tense about modifications

**Acceptable comments explain current behavior without referencing changes:**
- Acceptable: "# Multiply by 3x" instead of "# Reduced factor from 5x to 3x"
- Acceptable: "# Handle edge case" instead of "# Fixed edge case bug"

---

### poor_naming

- File, class, function, function parameter, and constant names should follow the format and naming standards that are currently dominant in the code base (especially within the same file or folder), or the style guide if one exists
- In the absence of existing code or style guide, common naming standards for the given programming language should be used
- Function names should be descriptive of what the function does. A person reading the function name without seeing its implementation should be able to get a sense of its purpose
- We don't impose any specific criteria on the length of names. If the existing code base uses many abbreviated names, new code should follow that. Or if it uses a lot of long, verbose names, this similarly should be followed
- If a component's functionality is significantly changed, the name of the component should be updated to reflect the new functionality, if it is not already clear from the context

**Exceptions:** Short names for local variables (especially as allowed for in a style guide) are usually okay.

---

### repetitive_or_duplicate_code

**Examples:**
- A non-trivial calculation or piece of logic is repeated in multiple places within a file
- New code is introduced by the diff to accomplish a certain functionality, but there is an existing function in the code base that already implements the same functionality, or could be easily generalized to accomplish the desired functionality
- A file is duplicated (make exceptions for cases where duplication may be necessary such as test files)
- A significant amount of code is introduced which duplicates functionality from standard or well-known libraries
- Multiple functions format or build the same string or data structure in the same way without using a shared helper function
- This is particularly common in tests, where multiple test cases may duplicate setup or validation logic that could be shared (e.g. as a fixture). It is important to flag such cases as a MAJOR issue!

**Exceptions:**
- Do not flag duplication between legacy and new implementations when the codebase is clearly undergoing a migration or maintaining multiple versions for compatibility
- Do not flag duplication across different architectural layers or modules when the duplication serves to maintain proper separation of concerns

---

### refactoring_needed

- Functions that have gotten long (> 50 lines) and are mixing multiple concerns and/or combining several different steps should be broken up. (Typically by using helper functions and/or separate classes to encapsulate individual concerns.)
- Classes or files that are combining different concerns should be broken up, such that each class / file only deals with one primary concern.
- This also includes structures that are unsafe (ex: returning a type that has an error state rather than raising an exception).
- Using primitive types (strings, integers, etc) to represent domain-level data--actual data types should be preferred instead, even if they simply inherit from the built-in types, as it makes the code more readable.
- Using an if/elif/.../else construct where you could use a match statement instead (eg, to dispatch on an enum value)

Note: we don't impose any minimal or maximal length on a class or file. Classes and files are ok to be long, as long as they only deal with a single concern.

**Examples:**
- New functionality that is orthogonal to the existing functionality in a function is inserted into the existing function's body instead of being separated out into its own function
- A class mixes two different use cases that could be separated into two classes
- A function that returns a value that can be either a valid result or an error state (e.g. None, False, -1) instead of raising an exception for the error case. This is bad because the caller can forget to check for the error state.
- A class that has a "name" attribute that is just a string, instead of having a proper Name class (eg, that inherits from NonEmptyString).
- A class with a bare string or uuid as an "id" attribute, instead of having a proper ID class
- An if/elif/.../else construct that dispatches on the value of an enum, instead of using a match statement (in languages that support it)

---

### test_coverage

- If the diff introduces significant new functionality, and the code base has existing unit and/or integration tests, new tests should be added to cover the new functionality
- If the diff changes the behavior of existing functionality that is covered by automated tests, those tests should be updated to reflect the new behavior
- If the diff contains a bug fix, and the code base has existing unit and/or integration tests, a regression test should be added for the bug

**Exceptions:** 
- Syntactical or logical issues in tests will be raised in other issue types and do not belong in this category.
- Changes *to the test code itself* (ex: to a conftest.py, testing_utils.py, test_*.py or *_test.py file) do not require test coverage (they will be executed when the tests run).

---

### test_quality

Any tests added in the diff should be of high quality individually, and should collectively create a high-quality test suite. This means:
- Avoid pointless and trivial tests
- Avoid creating lots of highly repetitive tests (parameterize the test or check all cases in a single test instead of making a separate test for each case, when appropriate)
- Ensure that common test code is factored out into fixtures
- Ensure that existing fixtures are used (when applicable)
- Ensure that tests are robust (ex: wait for conditions to be met rather than using hard-coded sleep statements, use appropriate timeouts, avoid flakiness)
- Ensure that the overall test suite for the changes is comprehensive and covers the new functionality well, but without creating more tests than necessary 
- Ensure that functionality is tested with unit tests whenever possible, only creating a small number of slower integration tests when necessary
- Ensure that multiple integration tests for similar functionality are serving unique purposes and are not overly repetitive or duplicative
- Ensure that the tests are as fast and simple as possible
- Ensure that individual tests are clearly named and easy to understand

---

### resource_leakage

Focus on system resources that require explicit cleanup: file handles, network connections, database connections, memory allocations, and similar OS-level resources.

These resources must be reliably freed even if exceptions occur.

For these system resources, cleanup should use try/finally blocks, context managers (with statements), or RAII patterns.

Also look for reference management issues: objects being cleaned up while still holding references elsewhere, or cleanup operations (like garbage collection) called before removing all references to the object.

**Examples:**
- A file or socket connection is opened but not reliably closed
- A database transaction is started but not committed or rolled back in all code paths
- Memory is allocated but not freed (in languages with manual memory management)
- An object's cleanup method triggers garbage collection while the object is still referenced in a global data structure, preventing proper cleanup

**Exceptions:**
- Animation loops, timers, and intervals that are controlled by boolean flags or cleared by ID are not resource leaks if they have proper stop mechanisms
- Event listeners that are meant to persist for the lifetime of the application
- Resources that are automatically cleaned up by garbage collection (unless they hold system resources)

---

### dependency_management

- Check all import statements in new or modified files. If new code imports a library or package that is not part of the language's standard library, verify that the dependency is listed in the repository's dependency/requirement files (e.g., requirements.txt, pyproject.toml, package.json, Gemfile, etc.)
- If the diff removes the last remaining use of an external library or package, the dependency and/or requirement files in the repository should be updated to no longer include the library
- If the codebase uses a dependency for some functionality, the diff should avoid introducing other packages that provide the same functionality, unless there is a good reason to do so (e.g. the new package is significantly better maintained, has better performance, or is more secure)

**Exceptions:** Do not raise issues related to package versions or pinning unless it is a critical issue.

---

### insecure_code

- Look for hard-coded secrets such as API keys, passwords, tokens, or credentials in the diff
- Check for variable names containing: "token", "key", "secret", "password", "credential", "auth"
- Look for string literals that appear to be:
    - API keys or tokens (long alphanumeric strings, often 20+ characters)
    - Hexadecimal strings that could be tokens or keys
    - URLs with embedded credentials (e.g., "https://user:password@host")
    - Connection strings with passwords
- Flag any credentials or secrets that should be loaded from environment variables or configuration files instead

**Examples:**
- A variable named `api_key` or `auth_token` is assigned a hard-coded string value
- A connection string contains a hard-coded password
- A long hexadecimal string is assigned to a variable with "token" in its name
- An API request includes a hard-coded authentication header value

---

### fails_silently

Code that fails silently is code that ignores errors without reporting them or properly handling them.

This includes behaviors like catching exceptions without logging them as warnings/errors (or re-raising them), returning inappropriate default values during an error condition, returning None instead of raising an error when there is a legitimate error, or otherwise allowing errors to occur without any indication to the user or developer.

**Examples:**
- The code indiscriminately captures exceptions of all types (e.g. Exception) or multiple types and continues execution without taking any action to handle the error
- Overly broad "except" clauses that catch many different types of errors and simply continue execution (rather than raising it so that invalid states are not silently ignored)
- Any "except" clause that does *not* log the error (at least at "trace" level) and/or report it to an error tracking system (e.g. Sentry). Real error conditions should be logged at *least* at warning level, and anything that violates a program invariant (eg, is an unexpected condition) should generally be raised.
- Returning None or an inappropriate default value when an error occurs instead of raising an exception. This can lead to downstream errors that are harder to debug because the original error is obscured.
- Any except clause *must* either log the error (if it is handling the error), or re-raise the error (if it is not handling the error). If an except clause does neither of these things, it is a silent failure (it's ok if the logging is at trace level, but it must be present).
- The return value of a function that returns an error value in case of a failure is not checked by the caller

**Exceptions:**
- There are certain cases where broad exception handlers are acceptable, such as in an executor class or in a main loop that iterates over several tasks. Such cases should still properly log and report the errors
- Do not raise issues related to potential program crashes

---

### instruction_file_disobeyed

Explicit instructions in files such as .claude.md, CLAUDE.md, and AGENTS.md MUST be obeyed.

**Examples:**
- CLAUDE.md requests the use of single quotes only, but double quotes are used
- AGENTS.md requests that new versions be created on every database update, but a database entry is modified directly
- .claude.md says to always run the tests after making changes, but the agent did not run the tests

**Exceptions:**
- Instructions in the closest file _above_ a location take precedence. For example, when considering a file foo/bar.py, foo/CLAUDE.md takes precedence over CLAUDE.md
- Instructions only apply to the subtree below the file. For example, when considering a file foo/bar.py, foo/baz/CLAUDE.md does not apply
- Applicable instructions should ONLY be contravened in the case of explicit user request--but if the user does explicitly request something counter to the instruction files, this should not be reported as a disobeyed instruction file

---

### logic_error

Logic errors are flaws in the reasoning or flow of the code that would cause incorrect behavior.

**Look for:**
- Off-by-one errors in loops or array indexing
- Incorrect conditional logic (wrong operators, inverted conditions)
- Variable assignments that overwrite needed values
- Incorrect order of operations
- Missing or incorrect loop termination conditions
- Algorithms that don't match their intended purpose
- Missing, incorrect, or incomplete parameters to function/API calls that will cause the function to behave differently than intended (e.g., missing event masks, wrong flags, omitted required options)

**Pay special attention to control flow issues:**
- Early returns or breaks that prevent intended functionality from executing
- Functions that exit before completing their stated purpose
- Conditions that prevent code paths from being reached when they should be

Do not flag issues that are not clearly incorrect. For example, it's possible code is implemented in a suboptimal way, this is not an issue unless it is explicitly stated that the code should be optimal or implemented in a certain way.

---

### runtime_error_risk

Code patterns that are very likely to cause runtime errors during execution.

**Check for version compatibility issues:**
- Usage of function parameters, APIs, or language features that are only available in specific versions of the language, standard library, or external dependencies (e.g., a keyword argument added in Python 3.10 will cause TypeError on Python 3.8/3.9)

**Look for:**
- Potential null/None pointer dereferences
- Array/list access with potentially invalid indices
- Division by zero possibilities
- File operations without existence checks
- Network/IO operations without timeout or error handling
- Infinite loop conditions
- Memory allocation issues

**Check string encoding/decoding operations:**
- Calls to .encode() or .decode() without error handling (try/except or "errors" parameter) that could raise UnicodeEncodeError or UnicodeDecodeError, especially when processing untrusted or streamed data

**Look for operations with global side effects that could cause problems:**
- os.chdir() without proper restoration
- Modifying global state in ways that affect other code
- Operations that are not thread-safe when concurrency is present

**Catch clauses that are too broad and could hide runtime errors:**
- Almost all try/except blocks (for specific types of errors) should only span a single line, and should generally catch a single class of errors.

**Look for platform-specific incompatibilities:**
- Code that will fail when run on OSX or linux (it's ok to fail on Windows)

Only flag issues where there is clear evidence the code will fail or cause serious problems. Avoid speculating about potential issues in well-established language patterns or standard library usage.

---

### incorrect_algorithm

Code that implements an algorithm incorrectly for its stated purpose.

**Look for:**
- Any reimplementation of complex algorithms that should be imported from standard libraries or well-known packages (ex: use max flow from networkx instead of reimplementing it)
- Sorting algorithms with wrong comparison logic
- Search algorithms with incorrect termination
- Mathematical calculations with wrong formulas
- Data structure operations that don't maintain invariants
- Algorithms that don't handle edge cases (empty inputs, single elements)

Only flag issues that are clearly incorrect for the stated purpose of the algorithm, and describe the problem and correction in detail.

---

### error_handling_missing

Missing error handling for operations that could reasonably fail.

**Look for:**
- File I/O without exception handling
- Network requests without timeout/retry logic
- User input processing without validation
- External API calls without error checking
- Database operations without transaction handling

Only flag issues that are clearly incorrect, and avoid flagging issues where it is not a big problem (e.g. file I/O in a script may not need flagging while missing error handling for file I/O in long running or production systems should have error handling).

---

### syntax_issues

The diff should not contain any syntax errors that would prevent the code from running.

**CAREFULLY CHECK INDENTATION:** In Python and other indentation-sensitive languages, verify that all function definitions, class definitions, and code blocks maintain proper indentation levels. Dedenting a function body to the module level or similar indentation errors are critical syntax issues.

**Look for:**
- Broken indentation that would cause syntax errors
- Missing or mismatched brackets/braces/parentheses
- References to files/classes/functions that don't exist
- Removal of code that is still being referenced elsewhere

**Check function signatures match their usage:**
- If a function is modified to return different values (e.g., a single value vs. a tuple), all call sites must be updated accordingly

**Examples:**
- The diff breaks the indentation of a Python function or class, dedenting it incorrectly
- Code references a file, class or function that does not exist, or removes a file, class or function that is definitely still being referenced
- A function is changed to return a tuple of two values, but existing callers still expect only a single return value
- A function's return statement is removed but callers still expect a return value

---

### abstraction_violation

Code that breaks established abstraction boundaries within the codebase.

**Look for:**
- Direct access to internal data structures of classes/modules that should be encapsulated
- Bypassing public APIs to manipulate state or access internal functionality
- Mixing of concerns that should be separated by layers or modules
- Violating private vs. public interfaces (e.g., accessing private attributes or methods from outside their defining class/module)

**Examples:**
- A Python function directly accesses a private attribute, variable or function (prefixed with an underscore) from a different class or file
- A module modifies the internal state of another module directly instead of using and/or adding public API functions

**Exceptions:** Unit tests that need to access internal state for verification purposes.

---

## Output Format

After your analysis when you are creating the final json file of issues, make a JSON record with each of the following fields (in order) for each issue you decide is valid to report, and append it as a new line to the final output json file:

- issue_type: the issue type code from above (e.g., "documentation_implementation_mismatch", "abstraction_abstraction", etc.)
- description: a complete description of the problem
- confidence_reasoning: the thought process for how confident you are that it is an issue at all
- confidence: a confidence score between 0.0 and 1.0 (1.0 = absolutely certain it is an issue, 0.0 = no confidence at all, should roughly be the probability that it is an actual issue to 1 decimal place)
- severity_reasoning: the thought process for how severe the issue is (assuming it were an issue, i.e., ignoring confidence)
- severity: one of "CRITICAL", "MAJOR", "MINOR", or "NITPICK", where
    - CRITICAL: must be fixed before merging; would cause major problems, crashes, or security issues
    - MAJOR: should be fixed before merging; would cause significant issues or confusion
    - MINOR: could be fixed before merging; would cause minor issues or inconveniences
    - NITPICK: optional to fix; mostly stylistic or very minor issues

