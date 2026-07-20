"""Prompts sent to testing agents and the integrator agent.

The prompt bodies live as Jinja2 templates under ``prompt_assets/``; this
module's job is to assemble the context dicts they render against. Prompt
edits land in the ``.j2`` files (so diffs stay focused on prose changes),
and the small bits of variable interpolation (outcome filenames, the
publish-outputs bash, etc.) flow through here.
"""

from pathlib import Path

from jinja2 import ChoiceLoader
from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import PackageLoader
from jinja2 import Template

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

# Default template names within ``prompt_assets`` (also the fallback names an
# override template can ``{% extends %}`` or ``{% include %}``).
_MAPPER_TEMPLATE = "mapper.j2"
_REDUCER_TEMPLATE = "reducer.j2"


def _load_override_template(template_path: Path) -> Template:
    """Load an override template file, backed by the packaged templates for extends/includes."""
    override_env = Environment(
        loader=ChoiceLoader(
            [
                FileSystemLoader(str(template_path.parent)),
                PackageLoader("imbue.mngr_tmr", "prompt_assets"),
            ]
        ),
        autoescape=False,
    )
    return override_env.get_template(template_path.name)


def _resolve_template(default_name: str, template_path: Path | None) -> Template:
    """Return the Jinja template to render.

    When ``template_path`` is None, use the packaged template named
    ``default_name``. Otherwise load the override file, backing it with a
    ``ChoiceLoader`` so the override may still ``{% extends %}`` or
    ``{% include %}`` the packaged templates by their default names.
    """
    if template_path is None:
        return _jinja_env.get_template(default_name)
    return _load_override_template(template_path)


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
    e2e_run_name: str | None = None,
    template_path: Path | None = None,
) -> str:
    """Build the prompt/initial message for a test-running agent.

    The prompt is generic: the test's docstring is the scope contract. When the
    test is an mngr e2e test, ``e2e_run_name`` is the base run name that gates the
    e2e-specific multi-run artifact-naming guidance (and is None otherwise).
    ``template_path`` overrides the packaged mapper template when provided.
    """
    flags_str = " ".join(pytest_flags)
    run_cmd = f"pytest {test_node_id}"
    if flags_str:
        run_cmd += f" {flags_str}"

    template = _resolve_template(_MAPPER_TEMPLATE, template_path)
    return template.render(
        run_cmd=run_cmd,
        outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        publish_snippet=_PUBLISH_OUTPUTS_SNIPPET,
        e2e_run_name=e2e_run_name,
    )


def build_task_file_mapper_prompt(
    task_id: str,
    kind: str,
    context_json: str,
    template_path: Path,
) -> str:
    """Build the mapper prompt for one task of a task-file recipe run.

    There is no packaged default template: the caller (a variant such as the
    minds spec-witnessing run) always supplies the template that anchors on
    its task semantics. The template renders against ``task_id``, ``kind``,
    ``context_json`` (the packet's context object as pretty-printed JSON),
    ``outcome_filename``, and ``publish_snippet``.
    """
    template = _load_override_template(template_path)
    return template.render(
        task_id=task_id,
        kind=kind,
        context_json=context_json,
        outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        publish_snippet=_PUBLISH_OUTPUTS_SNIPPET,
    )


def build_integrator_prompt(template_path: Path | None = None) -> str:
    """Build the integrator's initial message.

    The orchestrator has rsynced the per-test-agent output directories under
    ``REDUCER_INPUTS_DIRNAME`` in the integrator's work_dir, each subdir
    holding the test agent's ``test_output/<outcome.json>`` and (when commits
    were made) a ``branch.bundle``. The integrator must walk those
    subdirectories, apply the "should pull" predicate to filter qualifying
    agents, fetch the qualifying bundles into local branches, then cherry-pick.
    """
    template = _resolve_template(_REDUCER_TEMPLATE, template_path)
    return template.render(
        inputs_dirname=REDUCER_INPUTS_DIRNAME,
        mapper_outcome_filename=TESTING_AGENT_OUTCOME_FILENAME,
        reducer_outcome_filename=INTEGRATOR_OUTCOME_FILENAME,
        publish_snippet=_PUBLISH_OUTPUTS_SNIPPET,
    )
