"""Prompts sent to testing agents and the integrator agent.

Isolated in a dedicated module so that prompt changes are easy to spot in diffs
and easy to edit manually.
"""

PLUGIN_NAME = "test-map-reduce"


def build_test_agent_prompt(
    test_node_id: str,
    pytest_flags: tuple[str, ...],
    prompt_suffix: str = "",
) -> str:
    """Build the prompt/initial message for a test-running agent.

    Human-sanctioned: prompt is currently specific to mng's E2E tutorial tests.
    This should be made generic in the future, but is acceptable for now.
    """
    flags_str = " ".join(pytest_flags)
    run_cmd = f"pytest {test_node_id}"
    if flags_str:
        run_cmd += f" {flags_str}"

    prompt = f"""Run the test with: {run_cmd}

# If the test fails

You can record multiple kinds of changes -- they are not mutually exclusive (one
entry per kind, not per individual edit):

- "FIX_TEST": fix the test code (including fixtures).
- "FIX_IMPL": fix the program being tested.

Each change has a status: "SUCCEEDED" if the fix worked, "FAILED" if you tried
but could not complete it, or "BLOCKED" if the issue needs larger intervention
beyond this task. If you cannot determine what is wrong, report no changes.

# If the test succeeds - or after you fixed a failing test

Consider whether the test can be improved:

- Are the assertions good enough? Try to test by observing the actual effect of
  commands, like how a human would do when debugging interactively, by looking at
  e.g. files, git status, and so on. Avoid having too many specific assertions,
  because this can make the tests very brittle.

- Are there interesting edge cases worth covering?

- Is the code run in the pytest function close enough to the tutorial block?

- Does it make sense to add additional pytest functions that cover the same
  tutorial block? It is perfectly fine for two pytest functions to share the same
  block. Think about "happy" and "unhappy" paths -- for example, a test that
  verifies normal behavior and a separate test that verifies error handling or
  edge cases for the same command.

If you make improvements, record a change under the key "IMPROVE_TEST". If you
identify an improvement that needs a larger-scale intervention, use status
"BLOCKED". If no improvements are needed, leave the changes object empty.

# Examining the CLI transcript

After each test run, examine the generated CLI transcript (in the test output
directory). Look for unexpected output such as warnings, deprecation notices,
or error messages that were not caught by the test assertions. If you find
something concerning, consider whether the test should assert on it, or whether
the implementation should be fixed to avoid the warning.

# Inspecting tutorial blocks

Each of those tests are also associated with a tutorial block in
libs/mng/tutorials/mega_tutorial.sh; we divide the file into blocks by splitting
around empty lines. You'll find a reproduction of a tutorial block using the API
e2e.write_tutorial_block. When modifying the test, you should normally keep the
tutorial block unchanged: they should match exactly with the block in the tutorial
file (modulo leading whitespaces).

However, try to think if tutorial itself could be wrong or outdated. This should be
a rare case - often the tutorial block is a bit too concise to be run as-is, and
that may be intentional.

If you do think that the tutorial block is wrong or outdated, update both the
tutorial block in the mega_tutorial.sh file and the test code itself, and record
a change under the key "FIX_TUTORIAL".

# Committing your changes

IMPORTANT: Each change kind MUST get its own separate commit. Changes of the same
kind should be combined in one commit. The commit message MUST start with the kind
in brackets. Examples:

  [FIX_TEST] Fix assertion to check exit code instead of stdout
  [FIX_IMPL] Add missing timeout parameter to create command
  [IMPROVE_TEST] Add edge case for empty agent list

This means if you make both a FIX_TEST and a FIX_IMPL change, you should have
exactly two commits. Do NOT mix different kinds in the same commit.

# Running tests multiple times

You may run the test multiple times during your work (initial run, then after
each fix attempt). Each run should use a DIFFERENT --mng-e2e-run-name value
by appending a suffix to the base run name that was passed to you:

  First run:  --mng-e2e-run-name <base>_try_1
  Second run: --mng-e2e-run-name <base>_try_2
  ...and so on.

This ensures each run's artifacts (transcripts, recordings) are kept separately.
Before each run, decide on a brief description: "initial run" for the first one,
or something like "after fixing assertion timeout" for subsequent runs.

# Writing the result

Write the result atomically to avoid races with the orchestrator reading it:
1. First write to $MNG_AGENT_STATE_DIR/plugin/{PLUGIN_NAME}/result.json.draft
2. Then rename (mv) the .draft file to result.json

The schema is:

{{"changes": {{"FIX_TEST": {{"status": "SUCCEEDED", "summary_markdown": "Fixed assertion"}}}},
 "errored": false,
 "tests_passing_before": false,
 "tests_passing_after": true,
 "summary_markdown": "Fixed test assertion and verified it passes.",
 "test_runs": [
   {{"run_name": "<base>_try_1", "description_markdown": "initial run"}},
   {{"run_name": "<base>_try_2", "description_markdown": "after fixing assertion timeout"}}
 ]}}

Fields:
- changes: object keyed by change kind (IMPROVE_TEST, FIX_TEST, FIX_IMPL,
  FIX_TUTORIAL). Each value has status (SUCCEEDED, FAILED, BLOCKED) and
  summary_markdown. One entry per kind -- do not duplicate kinds.
- errored: true only for infrastructure errors that prevented you from working.
- tests_passing_before: were tests passing before you made any changes?
- tests_passing_after: are tests passing now, after all your changes?
- summary_markdown: overall markdown summary of what happened.
- test_runs: list of objects, one per test run, in order. Each has run_name
  (matching the --mng-e2e-run-name used) and description_markdown (brief
  description of what this run was for).
"""
    if prompt_suffix:
        prompt += f"\n{prompt_suffix}\n"
    return prompt


def build_integrator_prompt(
    fix_branches: list[str],
) -> str:
    """Build the prompt/initial message for the integrator agent."""
    branch_list = "\n".join(f"  - {b}" for b in fix_branches)
    return f"""Integrate the following branches into a single linear commit stack:
{branch_list}

# Strategy

Use cherry-pick (NOT merge) to build a clean linear history. The goal is a
branch with a flat list of commits that is easy to review.

# Steps

1. For each branch listed above, inspect the commits. Each branch should have
   commits prefixed with a change kind in brackets, like [FIX_TEST], [FIX_IMPL],
   [IMPROVE_TEST], or [FIX_TUTORIAL].

2. Collect all commits into two groups:
   a) "Test/doc" commits: those tagged [FIX_TEST], [IMPROVE_TEST], or [FIX_TUTORIAL].
   b) "Impl" commits: those tagged [FIX_IMPL].

3. Cherry-pick in this order:
   a) FIRST: cherry-pick all test/doc commits and squash them into a SINGLE commit.
      Use a commit message like: "[TEST/DOC] Combined test and doc fixes from N agents"
   b) THEN: cherry-pick each [FIX_IMPL] commit individually, keeping them as
      separate commits. Before cherry-picking, READ the commit messages of all
      FIX_IMPL commits and rank them by priority (most impactful / most important
      fix first). Cherry-pick in that priority order.

4. If a cherry-pick has conflicts, try to resolve them. If you cannot resolve
   a conflict for a particular branch, skip it and record it as failed.

5. After cherry-picking, record the commit hashes using `git rev-parse HEAD` after
   each step (the squashed commit and each impl commit).

6. Write the result to $MNG_AGENT_STATE_DIR/plugin/{PLUGIN_NAME}/result.json with:
{{"squashed_branches": ["branch1", "branch2"], "squashed_commit_hash": "abc1234", "impl_priority": ["branch3"], "impl_commit_hashes": {{"branch3": "def5678"}}, "failed": ["branch4"]}}

- squashed_branches: list of branch names whose test/doc commits were squashed
- squashed_commit_hash: the commit hash of the squashed test/doc commit (short hash is fine)
- impl_priority: list of impl branch names in priority order (highest first)
- impl_commit_hashes: mapping of each impl branch name to its commit hash on the integrated branch
- failed: list of branch names that could not be integrated
"""
