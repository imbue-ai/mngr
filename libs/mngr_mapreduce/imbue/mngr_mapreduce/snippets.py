"""Reusable bash snippets that recipe prompts can interpolate.

Every map-reduce agent (mapper or reducer) ends its run by publishing an
outputs archive at a framework-defined path on the host. The framework
polls for that archive to detect completion and extracts it on arrival.
These helpers produce the bash agents include in their prompts to honor
that contract.
"""

# Subpath under ``$MNGR_AGENT_STATE_DIR`` where the framework looks for
# every agent's outputs archive. The agent's prompt instructs it to write
# the final tarball at ``$MNGR_AGENT_STATE_DIR/<_ARCHIVE_SUBDIR>/outputs.tar.gz``.
# Single fixed path regardless of recipe, so the orchestrator and the agent
# agree on where to look without threading the recipe name through.
ARCHIVE_SUBDIR = "plugin/mapreduce"
ARCHIVE_FILENAME = "outputs.tar.gz"
ARCHIVE_SUBPATH = f"{ARCHIVE_SUBDIR}/{ARCHIVE_FILENAME}"


def publish_outputs_snippet() -> str:
    """Bash that packages ``.test_output`` into the outputs archive.

    The agent runs this from the git repo root. If the agent made commits
    on its branch beyond the base, a ``branch.bundle`` is included in the
    archive. Writes via a ``.tmp`` sibling and renames on completion so
    the orchestrator never reads a half-written archive. Recipes embed
    this verbatim in their final-step instructions.
    """
    return f"""```bash
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


def atomic_write_snippet(rel_path: str, content_var: str = "OUTCOME_JSON") -> str:
    """Bash that atomically writes ``$content_var`` to ``rel_path`` via .draft + mv.

    ``rel_path`` is relative to the git repo root. The orchestrator and any
    downstream consumer can read the final path without seeing a partial
    write. Recipes use this for any outcome-style files they put inside
    the outputs archive.
    """
    return f"""```bash
printf '%s' "${content_var}" > "{rel_path}.draft"
mv "{rel_path}.draft" "{rel_path}"
```"""
