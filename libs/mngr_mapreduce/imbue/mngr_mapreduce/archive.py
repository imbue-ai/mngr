"""Constants defining where map-reduce agents publish their outputs archive.

Both the orchestrator (when polling for the archive and extracting it) and
the recipe (when telling agents how to produce the archive) need to agree
on the path. This module is the single source of that agreement and
intentionally contains no logic -- the bash that actually writes the
archive lives recipe-side because the prompt is recipe-side.
"""

# Subpath under ``$MNGR_AGENT_STATE_DIR`` where the framework looks for
# every agent's outputs archive. Recipes embed this in their prompts so the
# agent writes the tarball to exactly this location.
ARCHIVE_SUBDIR = "plugin/mapreduce"
ARCHIVE_FILENAME = "outputs.tar.gz"
ARCHIVE_SUBPATH = f"{ARCHIVE_SUBDIR}/{ARCHIVE_FILENAME}"
