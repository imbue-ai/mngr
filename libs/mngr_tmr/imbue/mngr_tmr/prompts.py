"""Prompts sent to testing agents and the integrator agent.

Isolated in a dedicated module so that prompt changes are easy to spot in diffs
and easy to edit manually.
"""

from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_mapreduce.snippets import publish_outputs_snippet

TESTING_AGENT_OUTCOME_FILENAME = "testing_agent_outcome.json"
INTEGRATOR_OUTCOME_FILENAME = "integrator_outcome.json"

# Subdirectory of the integrator agent's work_dir into which the orchestrator
# rsyncs the local output directory before kicking the integrator off. Just an
# alias for the framework's neutral name so the existing prompt code reads
# the same.
INTEGRATOR_INPUTS_DIRNAME = REDUCER_INPUTS_DIRNAME


def build_test_agent_prompt(
    test_node_id: str,
    pytest_flags: tuple[str, ...],
) -> str:
    """Build the prompt/initial message for a test-running agent.

    Human-sanctioned: prompt is currently specific to mngr's E2E tutorial tests.
    This should be made generic in the future, but is acceptable for now.
    """
    flags_str = " ".join(pytest_flags)
    run_cmd = f"pytest {test_node_id}"
    if flags_str:
        run_cmd += f" {flags_str}"

    publish_snippet = publish_outputs_snippet()

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

# Guidelines for test quality

When writing or improving tests, follow these principles:

**Run the actual commands from the script block.** The test must run commands that
match the script block as closely as possible. For example, if the script block
demonstrates `mngr create --foo`, the test must run `mngr create --foo` (with
optional extra flags) -- it must NOT simply run `mngr create --help` and verify
that `--foo` is a supported flag. The test fixture already sets up an isolated
environment, so using hardcoded agent names is fine.

**Verify the actual behavior, not just surface-level output.** The script blocks
usually don't contain verification code, but the test must verify the exact
desired behavior as thoroughly as possible. For example, if a script block creates
an agent in a specific directory, it is not sufficient to only verify that the
agent appears in the result of `mngr list` -- you must also verify that the agent
is running in that directory, e.g. by running `mngr exec $agent_name pwd` and
checking its output. Think about what the command is supposed to accomplish and
assert on the concrete effects.

**Add comments to transcript commands.** The `e2e.run()` method accepts an
optional `comment` parameter that is recorded in the transcript above the command
(as `# ...` lines). Use this to annotate each command with a brief description of
what it does. Reuse comments from the tutorial script block where available.

# Examining the CLI transcript

After each test run, examine the generated CLI transcript (in the test output
directory). Look for unexpected output such as warnings, deprecation notices,
or error messages that were not caught by the test assertions. If you find
something concerning, consider whether the test should assert on it, or whether
the implementation should be fixed to avoid the warning.

# Inspecting tutorial blocks

Each of those tests are also associated with a tutorial block in
libs/mngr/imbue/mngr/resources/mega_tutorial.sh; we divide the file into blocks by splitting
around empty lines. You'll find a reproduction of a tutorial block using the API
e2e.write_tutorial_block. When modifying the test, you should normally keep the
tutorial block unchanged: they should match exactly with the block in the tutorial
file (modulo leading whitespaces).

However, try to think if tutorial itself could be wrong or outdated. This should be
a rare case - often the tutorial block is a bit too concise to be run as-is, and
that may be intentional.

If you do think that the tutorial block is wrong or outdated, update both the
tutorial block in the mega_tutorial.sh resource file and the test code itself, and record
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
each fix attempt). Each run should use a DIFFERENT --mngr-e2e-run-name value
by appending a suffix to the base run name that was passed to you:

  First run:  --mngr-e2e-run-name <base>_try_1
  Second run: --mngr-e2e-run-name <base>_try_2
  ...and so on.

This ensures each run's artifacts (transcripts, recordings) are kept separately.
Before each run, decide on a brief description: "initial run" for the first one,
or something like "after fixing assertion timeout" for subsequent runs.

# Writing the result

Write the result atomically to avoid races with the orchestrator reading it:
1. First write to .test_output/{TESTING_AGENT_OUTCOME_FILENAME}.draft (relative to the git repo root)
2. Then rename (mv) the .draft file to .test_output/{TESTING_AGENT_OUTCOME_FILENAME}

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
  (matching the --mngr-e2e-run-name used) and description_markdown (brief
  description of what this run was for).

# Publishing the outputs archive

After the outcome file is written, package the outputs and publish them
where the orchestrator can find them. Run this from the git repo root:

{publish_snippet}

This MUST be the very last step. The orchestrator polls for outputs.tar.gz
and treats its appearance as the signal that you are done. The .tmp + rename
sequence prevents the orchestrator from reading a half-written archive. Omit
branch.bundle when you made no commits beyond the base branch.

# Important: do not ask for user input

For this initial request, do NOT ask the user for any input or clarification.
Work autonomously. If something is unclear or you are blocked, produce a result
with the appropriate change status set to "BLOCKED" and explain in the
summary_markdown. If the user sends follow-up messages later, you may ask them
questions at that point.
"""
    return prompt


_INTEGRATOR_OUTCOME_SECTION = f"""# Writing the outcome and publishing outputs

After cherry-picking, write the result atomically to avoid races with the
orchestrator reading it:

1. First write to .test_output/{INTEGRATOR_OUTCOME_FILENAME}.draft (relative to the git repo root)
2. Then rename (mv) the .draft file to .test_output/{INTEGRATOR_OUTCOME_FILENAME}

The schema is:

{{"squashed_branches": ["branch1", "branch2"],
 "squashed_commit_hash": "abc1234",
 "impl_priority": ["branch3"],
 "impl_commit_hashes": {{"branch3": "def5678"}},
 "failed": ["branch4"]}}

Fields:
- squashed_branches: branch names whose test/doc commits were squashed
- squashed_commit_hash: commit hash of the squashed test/doc commit (short is fine)
- impl_priority: impl branch names in priority order, highest first
- impl_commit_hashes: mapping of impl branch name to its commit hash on the integrated branch
- failed: branch names that could not be integrated

Then, finally, publish the outputs archive. Run this from the git repo root:

{publish_outputs_snippet()}

This MUST be the very last step. The orchestrator polls for outputs.tar.gz
and treats its appearance as the signal that you are done. The .tmp + rename
sequence prevents the orchestrator from reading a half-written archive.
"""


_INTEGRATOR_CHERRY_PICK_INSTRUCTIONS = """# Cherry-pick strategy

Use cherry-pick (NOT merge) to build a clean linear history. The goal is a
branch with a flat list of commits that is easy to review.

1. For each branch in the list, inspect the commits. Each branch should have
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
"""


_INTEGRATOR_DO_NOT_ASK = """# Important: do not ask for user input

For this initial request, do NOT ask the user for any input or clarification.
Work autonomously. If a cherry-pick has conflicts you cannot resolve, skip that
branch and record it as failed. If the user sends follow-up messages later, you
may ask them questions at that point.
"""


def build_integrator_prompt() -> str:
    """Build the integrator's initial message.

    The orchestrator has rsynced the per-test-agent output directories under
    ``INTEGRATOR_INPUTS_DIRNAME`` in the integrator's work_dir, each subdir
    holding the test agent's ``test_output/<outcome.json>`` and (when commits
    were made) a ``branch.bundle``. The integrator must walk those
    subdirectories, apply the "should pull" predicate to filter qualifying
    agents, fetch the qualifying bundles into local branches, then cherry-pick.
    """
    return f"""Integrate fix branches from the test agents whose outputs have been
uploaded into ``{INTEGRATOR_INPUTS_DIRNAME}/`` (a sibling of the current
directory's contents, in the integrator's work_dir). Each subdirectory
``{INTEGRATOR_INPUTS_DIRNAME}/<agent_name>/`` contains the test agent's
``test_output/{TESTING_AGENT_OUTCOME_FILENAME}`` and (when it made commits)
a ``branch.bundle``.

# Discover and fetch qualifying branches

Run this from the git repo root to filter and fetch the branches whose
agents reported a real fix (and didn't regress the test suite):

```bash
set -euo pipefail

INPUTS_DIR="{INTEGRATOR_INPUTS_DIRNAME}"
FETCHED_BRANCHES=()

should_pull() {{
    python3 - "$1" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1]))
if data.get("errored"):
    sys.exit(1)
changes = data.get("changes") or {{}}
if not any((c or {{}}).get("status") == "SUCCEEDED" for c in changes.values()):
    sys.exit(1)
if data.get("tests_passing_before") is True and data.get("tests_passing_after") is not True:
    sys.exit(1)
sys.exit(0)
PYEOF
}}

bundle_branch() {{
    git bundle list-heads "$1" \\
        | awk 'NR==1 {{ sub(/^refs\\/heads\\//, "", $2); print $2 }}'
}}

for agent_dir in "$INPUTS_DIR"/*/; do
    outcome="$agent_dir/test_output/{TESTING_AGENT_OUTCOME_FILENAME}"
    bundle="$agent_dir/branch.bundle"
    [ -f "$outcome" ] || continue
    [ -f "$bundle" ] || continue
    if ! should_pull "$outcome"; then
        echo "Skipping $agent_dir (did not meet the should-pull predicate)"
        continue
    fi
    branch=$(bundle_branch "$bundle")
    if [ -z "$branch" ]; then
        echo "Skipping $agent_dir (could not read branch ref from bundle)"
        continue
    fi
    echo "Fetching $branch from $bundle"
    git fetch --no-tags "$bundle" "+$branch:$branch"
    FETCHED_BRANCHES+=("$branch")
done

printf '%s\\n' "${{FETCHED_BRANCHES[@]}}"
```

The branches you must cherry-pick are exactly the printed ones. If no
branches qualify, write an outcome describing that (no squashed/impl
commits) and proceed straight to publishing the outputs archive.

{_INTEGRATOR_CHERRY_PICK_INSTRUCTIONS}

{_INTEGRATOR_OUTCOME_SECTION}

{_INTEGRATOR_DO_NOT_ASK}"""
