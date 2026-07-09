"""End-to-end smoke of the full minds Electron workspace lifecycle.

Drives the real Electron app via Playwright (CDP) through the whole user
journey the desktop client exists for, against a *local Docker* workspace
created from the forever-claude-template:

  1. create a Docker workspace (local compute) with Imbue-Cloud AI creds so
     the agent can actually answer,
  2. send a chat message and wait for the agent's reply,
  3. open a terminal panel in the dockview,
  4. navigate back to the home/landing screen (via the chrome Home button),
  5. open that workspace's settings and destroy it (the versioned
     ``POST /api/v1/workspaces/<id>/destroy`` flow), confirming it leaves the
     landing list.

This complements ``test_snapshot_resume.py::test_create_apikey_workspace_and_chat_via_electron``
(which asserts the create + chat steps). The reusable flow lives in
``e2e_workspace_runner.run_full_workspace_flow``;
this script is the thin CLI wrapper that picks names + an env and reports.

Run it headless on Linux via ``just minds-test-electron-flow`` (wraps this in
``xvfb-run``). It inherits whatever minds env is activated; activate one with a
logged-in account first, e.g. ``eval "$(uv run minds env activate dev-josh-1)"``.

This is an operator/debug harness, not a pytest test: chat replies depend on
live AI creds and Docker, which are not available on the standard unit/CI
runners. The create + chat path is the one crystallized as a pytest test
(``test_snapshot_resume.py::test_create_apikey_workspace_and_chat_via_electron``).
"""

import os
import sys

from loguru import logger

from imbue.minds.desktop_client.e2e_workspace_runner import configure_logging
from imbue.minds.desktop_client.e2e_workspace_runner import destroy_agent_best_effort
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.desktop_client.e2e_workspace_runner import resolve_fct_path
from imbue.minds.desktop_client.e2e_workspace_runner import run_full_workspace_flow
from imbue.minds.desktop_client.fct_worktree import materialize_paired_fct_worktree
from imbue.mngr.utils.testing import get_short_random_string


def main() -> None:
    configure_logging()
    if not os.environ.get("MINDS_ROOT_NAME"):
        logger.error('No MINDS_ROOT_NAME activated. Run: eval "$(uv run minds env activate dev-josh-1)"')
        sys.exit(2)
    # Keep the local-Docker provider on the stock runtime and silence Modal noise.
    # MINDS_DOCKER_RUNTIME_DEFAULT pins the create form / API default to runc so
    # minds never stacks the `docker_runsc` create-template -- the only way runsc
    # gets selected. (A provider-config env var like
    # MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME cannot help: an explicitly stacked
    # template's docker_runtime outranks it.) The Electron child inherits this via
    # _build_electron_env, which copies os.environ.
    os.environ["MINDS_DOCKER_RUNTIME_DEFAULT"] = "RUNC"
    os.environ["MNGR__PROVIDERS__MODAL__IS_ENABLED"] = "false"

    # Materialize the paired FCT worktree (clone paired branch or main + vendor
    # this mngr checkout) if it is not already present, then resolve it. A
    # pre-existing operator worktree is left untouched.
    materialize_paired_fct_worktree()
    fct_path = resolve_fct_path()
    workspace_name = f"flowtest-{get_short_random_string()}"
    token = f"flowtok-{get_short_random_string()}"
    debug_port = find_free_port()
    logger.info("Workspace: {}; FCT: {}; CDP port: {}", workspace_name, fct_path, debug_port)

    results: dict[str, str] = {}
    agent_id: str | None = None
    try:
        results, agent_id = run_full_workspace_flow(fct_path, workspace_name, token, debug_port)
    finally:
        # Always tear the host down even if a step failed. Destroy by the
        # canonical agent id when known -- destroying the host's last agent tears
        # down the host; the workspace name alone is the host name, which
        # `mngr destroy` does not match. Best-effort; safe if already gone.
        destroy_agent_best_effort(agent_id or workspace_name)

    logger.info("================ FLOW RESULTS ({}) ================", workspace_name)
    for step, outcome in results.items():
        logger.info("  {:<18} {}", step, outcome)
    failures = [s for s, o in results.items() if o != "PASS"]
    if failures or "STEP 1 create" not in results:
        logger.error("FLOW FAILED: {}", failures or "create never completed")
        sys.exit(1)
    logger.info("ALL STEPS PASSED for workspace {}", workspace_name)


if __name__ == "__main__":
    main()
