"""End-to-end test that drives the real Electron minds app to create a
local Docker workspace from the forever-claude-template (FCT) repo.

The actual flow -- launch Electron, drive the create form via Playwright
over CDP, wait for the workspace's ``system_interface`` dockview to
render -- lives in
``apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py`` so
that ``scripts/snapshot_minds_e2e_state.py`` can call the same code
without going through pytest (and, crucially, without destroying the
agent we want to snapshot).

This test is the assert-and-clean-up wrapper: it sets the minds env via
pytest's monkeypatch, picks a tmp_path for any FCT shallow clone, runs
the workspace-creation flow, and always destroys the resulting mngr
agent in a ``finally`` regardless of outcome.

The FCT source is resolved by the runner in three steps (first match wins):

1. ``<repo-root>/.external_worktrees/forever-claude-template/`` if that
   directory is a populated git working tree.
2. Otherwise, a shallow clone of the branch on the FCT public remote
   that matches the current mngr branch, into ``tmp_path``.
3. Otherwise, a shallow clone of FCT ``main`` into ``tmp_path``.

The test inherits whatever minds env the runner already activated. When
``MINDS_ROOT_NAME`` is unset, it defaults to the shared ``minds-staging``
tier (matches the repo-committed ``client.toml`` under
``apps/minds/imbue/minds/config/envs/staging/``).

Run locally:

    just minds-test-electron

Linux CI requires ``xvfb`` (the recipe wraps the invocation with
``xvfb-run -a``).
"""

from pathlib import Path

import pytest
from loguru import logger

from imbue.minds.desktop_client.e2e_workspace_runner import configure_logging
from imbue.minds.desktop_client.e2e_workspace_runner import create_workspace_via_electron
from imbue.minds.desktop_client.e2e_workspace_runner import destroy_agent_best_effort
from imbue.minds.desktop_client.e2e_workspace_runner import ensure_minds_env_defaults
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.desktop_client.e2e_workspace_runner import resolve_fct_path
from imbue.mngr.utils.testing import get_short_random_string


# Carrying only the resource marks the *test process* sees a host-side
# invocation of, after the test passes end-to-end:
#
# - `docker` (CLI) is invoked by the spawned `mngr create` subprocess to
#   start the container; the PATH-injected resource-guard wrapper catches it.
# - `rsync` is invoked by `mngr create` to overlay the FCT worktree onto
#   the internal clone; same PATH wrapper.
#
# Marks we deliberately do *not* carry, and why:
#
# - `tmux` -- the workspace agent's tmux session lives *inside* the docker
#   container, never on the host, so the host's tmux wrapper never ticks
#   the counter and the guard fires post-hoc with "marked tmux but never
#   invoked tmux".
# - `docker_sdk` -- the Python `docker` SDK guard is a wrapper around the
#   in-process SDK import, not a PATH wrapper, so it only sees uses from
#   *this* pytest process. mngr's docker SDK calls happen in the spawned
#   subprocess and never reach our SDK wrapper, so the mark fires the
#   same "marked but never invoked" check.
@pytest.mark.acceptance
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.minds_electron
@pytest.mark.timeout(900)
def test_create_local_docker_workspace_via_electron(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the Electron app to create a local Docker workspace from FCT.

    Asserts the workspace's ``system_interface`` dockview UI renders
    through the desktop client proxy. Cleans up the mngr agent in
    ``finally`` regardless of outcome.
    """
    configure_logging()
    # Route env-var defaults through monkeypatch so any values we inject get
    # reverted between tests; the runner's default `os.environ` setter would
    # leak `MINDS_ROOT_NAME` / `MINDS_CLIENT_CONFIG_PATH` into sibling tests.
    ensure_minds_env_defaults(setenv=monkeypatch.setenv)

    fct_path = resolve_fct_path(tmp_path)
    workspace_name = f"forever-{get_short_random_string()}"
    debug_port = find_free_port()
    logger.info("Workspace name: {}; CDP debug port: {}", workspace_name, debug_port)

    try:
        create_workspace_via_electron(fct_path, workspace_name, debug_port)
    finally:
        destroy_agent_best_effort(workspace_name)
