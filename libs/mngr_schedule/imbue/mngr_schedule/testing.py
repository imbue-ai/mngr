"""Shared test utilities for mngr-schedule release tests.

These helpers are used by test_schedule_add.py and test_schedule_run.py
for end-to-end tests that require real Modal credentials and network access.
"""

import importlib.metadata
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

from loguru import logger

from imbue.mngr.utils.testing import generate_test_environment_name

# Read the real home directory at import time, BEFORE any autouse fixture
# overrides HOME. We do NOT pass real HOME to subprocesses: doing so leaves
# them in a split-brain state where HOME points at the developer's real home
# but MNGR_HOST_DIR / MNGR_ROOT_NAME still point at the test tmp dir, which
# trips plugin code that assumes the host dir lives under $HOME (e.g.
# get_files_for_deploy's relative_to(user_home) call). We only use the real
# home to read ~/.modal.toml below and pass the tokens in as env vars.
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


def load_modal_creds_from_home() -> dict[str, str]:
    """Return MODAL_TOKEN_{ID,SECRET} loaded from the developer's active
    ~/.modal.toml profile, or an empty dict if the file is missing or
    doesn't define an active profile with both tokens.

    ~/.modal.toml is TOML with one section per profile; the active one
    has `active = true`. Modal's SDK reads this file normally, but tests
    run subprocesses under an isolated HOME (see the REAL_HOME comment),
    so the file becomes invisible -- we pull the tokens out here and
    pass them through as env vars instead.
    """
    modal_toml = REAL_HOME / ".modal.toml"
    if not modal_toml.is_file():
        return {}
    data = tomllib.loads(modal_toml.read_text())
    for profile in data.values():
        if not isinstance(profile, dict) or not profile.get("active"):
            continue
        token_id = profile.get("token_id")
        token_secret = profile.get("token_secret")
        if isinstance(token_id, str) and isinstance(token_secret, str):
            return {"MODAL_TOKEN_ID": token_id, "MODAL_TOKEN_SECRET": token_secret}
    return {}


def build_subprocess_env() -> dict[str, str]:
    """Build environment for subprocess calls that need Modal credentials.

    Keeps the test-isolated HOME so subprocess plugin code that assumes the
    mngr host_dir lives under $HOME (e.g. get_files_for_deploy's
    relative_to(user_home)) stays consistent. Modal credentials come from
    env vars in CI/offload; locally we fall back to reading them out of
    the developer's ~/.modal.toml and passing them in as
    MODAL_TOKEN_ID/MODAL_TOKEN_SECRET so HOME can remain isolated.

    Keeps the autouse-set MNGR_HOST_DIR / MNGR_ROOT_NAME so the subprocess
    mngr operates on an isolated tmp profile and does not load the repo's
    .mngr/settings.toml (which would trip the is_allowed_in_pytest=false
    guard). The Modal SSH key will be auto-generated on first use inside
    the tmp profile.

    Deliberately does NOT strip PYTEST_CURRENT_TEST: the Modal backend's
    TEST_ENV_PATTERN guard and the config is_allowed_in_pytest check rely
    on that marker, and evading them has leaked un-sweepable Modal envs
    in the past.
    """
    env = os.environ.copy()
    has_modal_env_creds = "MODAL_TOKEN_ID" in env and "MODAL_TOKEN_SECRET" in env
    if not has_modal_env_creds:
        env.update(load_modal_creds_from_home())
    # Ensure the prefix starts with mngr_test- so the Modal backend's guard
    # accepts it and the cleanup script can identify these environments.
    env["MNGR_PREFIX"] = f"{generate_test_environment_name()}-"
    return env


def resolve_modal_environment(deploy_output: str) -> str | None:
    """Extract the Modal environment name from schedule add output.

    Parses the 'env: <name>' from the deploy log line. Returns None
    if the environment cannot be determined.
    """
    match = re.search(r"env:\s*(\S+)\)", deploy_output)
    if match:
        return match.group(1)
    return None


def cleanup_modal_app(
    app_name: str,
    env: dict[str, str],
    modal_environment: str | None,
    *,
    cwd: Path | None = None,
) -> None:
    """Stop and clean up a Modal app created during testing.

    Both 'modal app list' and 'modal app stop' are given --env so they target
    the per-run Modal environment that the tests deploy into. Without --env,
    Modal queries the user's default environment and silently fails to find
    the test app, leaking Modal resources.

    ``cwd`` is passed through to the Modal subprocesses; callers that need
    Modal to see a specific working directory (for repo context etc.) should
    set it.
    """
    if modal_environment is None:
        logger.warning("Cannot clean up Modal app '{}': environment unknown", app_name)
        return
    try:
        list_result = subprocess.run(
            ["uv", "run", "modal", "app", "list", "--json", "--env", modal_environment],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=cwd,
        )
        if list_result.returncode == 0:
            apps = json.loads(list_result.stdout)
            for app in apps:
                if app.get("Description", "") == app_name:
                    app_id = app.get("App ID", "")
                    if app_id:
                        subprocess.run(
                            ["uv", "run", "modal", "app", "stop", app_id, "--env", modal_environment],
                            capture_output=True,
                            timeout=30,
                            env=env,
                            cwd=cwd,
                        )
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as exc:
        logger.warning("Failed to clean up Modal app '{}': {}", app_name, exc)


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
    command: str,
    args: str,
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
            command,
            "--args",
            args,
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
