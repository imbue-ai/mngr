"""Prompts sent to testing agents and the integrator agent.

The prompt bodies live as Jinja2 templates under ``prompt_assets/``; this
module's job is to assemble the context dicts they render against. Prompt
edits land in the ``.j2`` files (so diffs stay focused on prose changes),
and the small bits of variable interpolation (outcome filenames, the
publish-outputs bash, etc.) flow through here.
"""

from jinja2 import Environment
from jinja2 import PackageLoader

from imbue.mngr_mapreduce.launching import REDUCER_INPUTS_DIRNAME
from imbue.mngr_mapreduce.snippets import publish_outputs_snippet

TESTING_AGENT_OUTCOME_FILENAME = "testing_agent_outcome.json"
INTEGRATOR_OUTCOME_FILENAME = "integrator_outcome.json"

# Subdirectory of the integrator agent's work_dir into which the orchestrator
# rsyncs the local output directory before kicking the integrator off. Just an
# alias for the framework's neutral name so the existing prompt code reads
# the same.
INTEGRATOR_INPUTS_DIRNAME = REDUCER_INPUTS_DIRNAME

# Prompts are plain text, not HTML, so autoescaping is off. The templates
# never mix variable substitution with literal `{{`/`}}` -- empty-dict bash
# and JSON examples use single `{` `}` only -- so no `{% raw %}` blocks are
# needed.
_jinja_env = Environment(
    loader=PackageLoader("imbue.mngr_tmr", "prompt_assets"),
    autoescape=False,
)


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
        publish_snippet=publish_outputs_snippet(),
    )


def build_integrator_prompt() -> str:
    """Build the integrator's initial message.

    The orchestrator has rsynced the per-test-agent output directories under
    ``INTEGRATOR_INPUTS_DIRNAME`` in the integrator's work_dir, each subdir
    holding the test agent's ``test_output/<outcome.json>`` and (when commits
    were made) a ``branch.bundle``. The integrator must walk those
    subdirectories, apply the "should pull" predicate to filter qualifying
    agents, fetch the qualifying bundles into local branches, then cherry-pick.
    """
    template = _jinja_env.get_template("reducer.j2")
    return template.render(
        inputs_dirname=INTEGRATOR_INPUTS_DIRNAME,
        mapper_outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        reducer_outcome_filename=INTEGRATOR_OUTCOME_FILENAME,
        publish_snippet=publish_outputs_snippet(),
    )
