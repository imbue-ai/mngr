"""Shared test utilities for mngr-schedule release tests.

These helpers are used by test_schedule_add.py and test_schedule_run.py
for end-to-end tests that require real Modal credentials and network access.
"""

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


def build_subprocess_env() -> dict[str, str]:
    """Build environment for subprocess calls that need real Modal credentials.

    The autouse test fixture overrides HOME to a temp directory for test isolation.
    Subprocesses need the real HOME to find ~/.modal.toml, git config, mngr profiles,
    etc. We restore the real HOME and remove test isolation vars so the subprocess
    uses the real mngr configuration (which has the correct Modal environment).
    """
    env = os.environ.copy()
    env["HOME"] = str(REAL_HOME)
    # Remove test isolation vars that would interfere with the real mngr config
    env.pop("MNGR_HOST_DIR", None)
    env.pop("MNGR_PREFIX", None)
    env.pop("MNGR_ROOT_NAME", None)
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


def deploy_test_trigger(
    trigger_name: str,
    env: dict[str, str],
    *,
    provider: str = "modal",
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Deploy a test trigger via schedule add. Returns the subprocess result.

    Runs from REPO_ROOT so that git context is available (the autouse test
    fixture chdir's into tmp_path which is outside any git repo).
    """
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
            "test-agent echo --no-connect --await-ready --no-ensure-clean --branch :run-{DATE} -- hello-from-schedule-run",
            "--schedule",
            "0 3 * * *",
            "--provider",
            provider,
            "--no-auto-merge",
            "--verify",
            "none",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=REPO_ROOT,
    )
