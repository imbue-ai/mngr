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

The test inherits whatever minds env the operator has activated. When
``MINDS_ROOT_NAME`` is unset, the wrapper calls
``ensure_minds_env_defaults(setenv=monkeypatch.setenv)`` which sets the
shared ``minds-staging`` tier as the default (matches the repo-committed
``client.toml`` under ``apps/minds/imbue/minds/config/envs/staging/``).

Run locally:

    just minds-test-electron

Linux CI requires ``xvfb`` (the recipe wraps the invocation with
``xvfb-run -a``).
"""

import subprocess
from pathlib import Path

import pytest
import tomlkit
from loguru import logger

from imbue.minds.desktop_client.e2e_workspace_runner import _REPO_ROOT
from imbue.minds.desktop_client.e2e_workspace_runner import configure_logging
from imbue.minds.desktop_client.e2e_workspace_runner import create_workspace_via_electron
from imbue.minds.desktop_client.e2e_workspace_runner import destroy_agent_best_effort
from imbue.minds.desktop_client.e2e_workspace_runner import ensure_minds_env_defaults
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.desktop_client.e2e_workspace_runner import materialize_isolated_fct
from imbue.minds.desktop_client.e2e_workspace_runner import resolve_fct_path
from imbue.mngr.utils.testing import get_short_random_string


def _opt_into_pytest_config_guard(settings_path: Path) -> None:
    """Set ``is_allowed_in_pytest = true`` in a throwaway ``settings.toml``.

    mngr's config guard refuses to run under ``PYTEST_CURRENT_TEST`` unless every
    config file it loads opts in. This writes the file in place with no restore,
    so ``settings_path`` must live under ``tmp_path`` (or another throwaway tree
    such as a clone of FCT) -- never a real checkout.
    """
    doc = tomlkit.parse(settings_path.read_text()) if settings_path.exists() else tomlkit.document()
    doc["is_allowed_in_pytest"] = True
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(tomlkit.dumps(doc))


def _isolated_host_config_root(scratch_dir: Path) -> Path:
    """Build a throwaway git repo holding an opted-in copy of the repo's mngr config.

    The Electron app runs from the returned directory (passed as
    ``create_workspace_via_electron``'s ``host_config_dir``), so the host-side
    ``mngr`` it spawns -- ``mngr auth list`` for the account-discovery poll,
    ``mngr forward`` for the workspace proxy and agent discovery -- resolves its
    project config here instead of the real repo ``.mngr/``. We copy the repo's
    ``settings.toml`` verbatim (preserving e.g. the ``forward`` plugin and the
    provider config the proxy's agent discovery needs) and add the pytest opt-in,
    deliberately omitting any ``settings.local.toml``: the repo's committed
    ``is_allowed_in_pytest = false`` plus a developer's untracked
    ``.mngr/settings.local.toml`` are exactly what would otherwise trip the guard.
    ``git init`` makes this the worktree root mngr's project-config walk stops at.
    """
    root = scratch_dir / "mngr_host_config"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", str(root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    settings_path = root / ".mngr" / "settings.toml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text((_REPO_ROOT / ".mngr" / "settings.toml").read_text())
    _opt_into_pytest_config_guard(settings_path)
    return root


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
    # Route env-var defaults through `monkeypatch.setenv` so any
    # `MINDS_ROOT_NAME` / `MINDS_CLIENT_CONFIG_PATH` values the runner
    # injects get reverted between tests instead of leaking into siblings.
    # The runner intentionally has no default setter -- the snapshot
    # script (which runs in a throwaway sandbox) passes an
    # `os.environ`-mutating setter, which is exactly what we want to
    # avoid here.
    ensure_minds_env_defaults(setenv=monkeypatch.setenv)

    # No Modal creds in this local-Docker test, so the Electron-spawned `mngr`'s
    # provider discovery would otherwise log "Modal is not authorized" every ~10s.
    # The env is inherited by the Electron child via `_build_electron_env`.
    monkeypatch.setenv("MNGR__PROVIDERS__MODAL__IS_ENABLED", "false")

    # FCT's `[providers.docker]` block sets `docker_runtime = "runsc"` to harden
    # the local-docker provider with gVisor. CI runners (and most dev machines)
    # do not have runsc installed, so `docker run --runtime runsc` would fail
    # immediately with "unknown or invalid runtime name: runsc" and the test
    # times out waiting for the agent navigation that never happens. Override
    # back to the default runtime; FCT's settings.toml comment names this exact
    # env var as the supported escape hatch for CI / Modal.
    monkeypatch.setenv("MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME", "runc")

    # The Electron-spawned `mngr` loads two project-config trees under
    # PYTEST_CURRENT_TEST: the host-side tree (where `mngr auth list` / `mngr
    # forward` and the proxy's agent discovery run) and the FCT checkout's (where
    # `mngr create` runs). Both must opt into the pytest config guard or `mngr`
    # refuses to run, and neither opt-in lives in committed state. The two need
    # *different* configs -- the host side needs the repo's `.mngr` (its `forward`
    # plugin + provider config drive the proxy/discovery), `mngr create` needs
    # FCT's own templates -- and they differ only by cwd, so a single
    # MNGR_PROJECT_CONFIG_DIR cannot serve both. Instead of editing the real
    # files, we point the Electron host process at `host_config_root` (an
    # opted-in copy of the repo's `.mngr`) and hand `mngr create` `fct_path` (a
    # throwaway FCT tree) whose own opted-in `.mngr/settings.toml` rides into the
    # container.
    fct_path = materialize_isolated_fct(resolve_fct_path(tmp_path), tmp_path)
    _opt_into_pytest_config_guard(fct_path / ".mngr" / "settings.toml")
    host_config_root = _isolated_host_config_root(tmp_path)

    workspace_name = f"forever-{get_short_random_string()}"
    debug_port = find_free_port()
    logger.info("Workspace name: {}; CDP debug port: {}", workspace_name, debug_port)

    try:
        create_workspace_via_electron(fct_path, workspace_name, debug_port, host_config_dir=host_config_root)
    finally:
        destroy_agent_best_effort(workspace_name, config_project_dir=host_config_root / ".mngr")
