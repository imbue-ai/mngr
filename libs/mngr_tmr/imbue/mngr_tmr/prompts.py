"""Prompts sent to testing agents and the integrator agent.

The prompt bodies live as Jinja2 templates under ``prompt_assets/``; this
module's job is to assemble the context dicts they render against. Prompt
edits land in the ``.j2`` files (so diffs stay focused on prose changes),
and the small bits of variable interpolation (outcome filenames, the
publish-outputs bash, etc.) flow through here.
"""

from jinja2 import Environment
from jinja2 import PackageLoader

from imbue.mngr_mapreduce.archive import ARCHIVE_FILENAME
from imbue.mngr_mapreduce.archive import ARCHIVE_SUBDIR
from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME

TESTING_AGENT_OUTCOME_FILENAME = "testing_agent_outcome.json"
INTEGRATOR_OUTCOME_FILENAME = "integrator_outcome.json"

# Prompts are plain text, not HTML, so autoescaping is off. The templates
# never mix variable substitution with literal `{{`/`}}` -- empty-dict bash
# and JSON examples use single `{` `}` only -- so no `{% raw %}` blocks are
# needed.
_jinja_env = Environment(
    loader=PackageLoader("imbue.mngr_tmr", "prompt_assets"),
    autoescape=False,
)


# Bash that packages ``.test_output`` into the outputs archive. The agent
# runs this from the git repo root as the final step of both the mapper and
# reducer prompts. Writes via a ``.tmp`` sibling and renames on completion
# so the orchestrator never reads a half-written archive. ``ARCHIVE_SUBDIR``
# / ``ARCHIVE_FILENAME`` come from the framework so the bash and the
# orchestrator's polling agree on where to look.
_PUBLISH_OUTPUTS_SNIPPET = f"""```bash
ARCHIVE_DIR="$MNGR_AGENT_STATE_DIR/{ARCHIVE_SUBDIR}"
mkdir -p "$ARCHIVE_DIR"

STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

# Rename .test_output -> test_output inside the archive
cp -a .test_output "$STAGING/test_output"

# Include an incremental git bundle if any commits exist beyond the base.
# The bundle is created with the explicit branch name so the orchestrator
# can fetch ``$BRANCH:$BRANCH`` cleanly.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ -n "$(git rev-list --max-count=1 "$MNGR_GIT_BASE_BRANCH..$BRANCH" 2>/dev/null)" ]; then
    git bundle create "$STAGING/branch.bundle" "$MNGR_GIT_BASE_BRANCH..$BRANCH"
fi

TARBALL="$ARCHIVE_DIR/{ARCHIVE_FILENAME}"
tar -czf "$TARBALL.tmp" -C "$STAGING" .
mv "$TARBALL.tmp" "$TARBALL"
```"""


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

    template = _jinja_env.get_template("mapper.j2")
    return template.render(
        run_cmd=run_cmd,
        outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        publish_snippet=_PUBLISH_OUTPUTS_SNIPPET,
    )


def build_integrator_prompt() -> str:
    """Build the integrator's initial message.

    The orchestrator has rsynced the per-test-agent output directories under
    ``REDUCER_INPUTS_DIRNAME`` in the integrator's work_dir, each subdir
    holding the test agent's ``test_output/<outcome.json>`` and (when commits
    were made) a ``branch.bundle``. The integrator must walk those
    subdirectories, apply the "should pull" predicate to filter qualifying
    agents, fetch the qualifying bundles into local branches, then cherry-pick.
    """
    template = _jinja_env.get_template("reducer.j2")
    return template.render(
        inputs_dirname=REDUCER_INPUTS_DIRNAME,
        mapper_outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        reducer_outcome_filename=INTEGRATOR_OUTCOME_FILENAME,
        publish_snippet=_PUBLISH_OUTPUTS_SNIPPET,
    )
