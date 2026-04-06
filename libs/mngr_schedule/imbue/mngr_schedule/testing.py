"""Shared test utilities for mngr-schedule release tests.

These helpers are used by test_schedule_add.py and test_schedule_run.py
for end-to-end tests that require real Modal credentials and network access.
"""

import importlib.metadata
import json
import os
import subprocess
from pathlib import Path

from loguru import logger

# Read the real home directory at import time, BEFORE any autouse fixture
# overrides HOME. Subprocesses need the real HOME to find ~/.modal.toml,
# git config, mngr profiles, etc.
REAL_HOME: Path = Path.home()

# Capture the repo root at import time, BEFORE the autouse fixture chdir's
# into tmp_path (which is outside any git repo). Subprocesses that run
# mngr schedule commands need to be in a git repo for auto-merge and code
# packaging to work.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent


def _get_all_plugin_names() -> frozenset[str]:
    """Discover all installed mngr plugin entry point names."""
    return frozenset(ep.name for ep in importlib.metadata.entry_points(group="mngr"))


def build_disable_plugin_args(enabled_plugins: frozenset[str]) -> list[str]:
    """Build --disable-plugin CLI args for all plugins NOT in enabled_plugins.

    Scans installed mngr entry points and produces --disable-plugin flags
    for everything except the specified set. This gives subprocess tests
    explicit control over which plugins are active, matching the
    enabled_plugins fixture pattern used for in-process tests.
    """
    all_plugins = _get_all_plugin_names()
    to_disable = sorted(all_plugins - enabled_plugins)
    args: list[str] = []
    for name in to_disable:
        args.extend(["--disable-plugin", name])
    return args


def build_subprocess_env() -> dict[str, str]:
    """Build environment for subprocess calls that need real Modal credentials.

    Restores the real HOME so subprocesses can find ~/.modal.toml for Modal
    authentication. Sets MNGR_ROOT_NAME to an isolated value so mngr does
    NOT load the user's personal profile (which may contain plugin config
    fields incompatible with the deployed mngr version). The modal backend
    is still discoverable without user config via default backend resolution.
    """
    env = os.environ.copy()
    env["HOME"] = str(REAL_HOME)
    # Use an isolated mngr config namespace so we don't load the user's
    # personal settings (e.g. kanpan plugin config that the container
    # mngr version may not understand).
    env["MNGR_ROOT_NAME"] = "mngr-schedule-test"
    # Remove other test isolation vars
    env.pop("MNGR_HOST_DIR", None)
    env.pop("MNGR_PREFIX", None)
    # Remove pytest marker so mngr doesn't reject the call
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def cleanup_modal_app(app_name: str, env: dict[str, str]) -> None:
    """Stop and clean up a Modal app created during testing."""
    try:
        list_result = subprocess.run(
            ["uv", "run", "modal", "app", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if list_result.returncode == 0:
            apps = json.loads(list_result.stdout)
            for app in apps:
                if app.get("Description", "") == app_name:
                    app_id = app.get("App ID", "")
                    if app_id:
                        subprocess.run(
                            ["uv", "run", "modal", "app", "stop", app_id],
                            capture_output=True,
                            timeout=30,
                            env=env,
                        )
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        logger.warning("Failed to clean up Modal app '{}'", app_name)


def remove_test_trigger(
    trigger_name: str,
    env: dict[str, str],
    enabled_plugins: frozenset[str],
    *,
    provider: str = "modal",
) -> None:
    """Remove a test trigger via mngr schedule remove. Best-effort cleanup."""
    disable_args = build_disable_plugin_args(enabled_plugins)
    try:
        subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "schedule",
                "remove",
                trigger_name,
                "--provider",
                provider,
                "--force",
                *disable_args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Timed out removing test trigger '{}'", trigger_name)


def deploy_test_trigger(
    trigger_name: str,
    env: dict[str, str],
    enabled_plugins: frozenset[str],
    *,
    provider: str = "modal",
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Deploy a test trigger via schedule add. Returns the subprocess result.

    enabled_plugins controls which mngr plugins are active in the subprocess.
    All other installed plugins are disabled via --disable-plugin flags.

    Runs from REPO_ROOT so that git context is available (the autouse test
    fixture chdir's into tmp_path which is outside any git repo).
    """
    disable_args = build_disable_plugin_args(enabled_plugins)
    return subprocess.run(
        [
            "uv",
            "run",
            "mngr",
            "schedule",
            "add",
            trigger_name,
            "--command",
            "create",
            "--args",
            "test-agent 'sleep 60' --no-connect --no-ensure-clean --context /tmp --provider modal --branch :run-{DATE}",
            "--schedule",
            "0 3 * * *",
            "--provider",
            provider,
            "--no-auto-merge",
            "--full-copy",
            "--exclude-user-settings",
            "--exclude-project-settings",
            "--verify",
            "none",
            *disable_args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=REPO_ROOT,
    )
