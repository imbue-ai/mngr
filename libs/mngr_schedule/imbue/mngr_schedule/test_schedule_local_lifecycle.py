"""Release test for the local provider schedule lifecycle.

Exercises the real `crontab` binary end-to-end: add a trigger via the CLI,
assert the mngr marker appears in `crontab -l`, remove the trigger, assert
the marker is gone.

Complements the in-process tests which inject fake crontab readers/writers
(see `cli/remove_test.py` and `implementations/local/deploy_test.py`):
those cover the pure crontab text manipulation, while this test is the
only coverage that proves the CLI actually invokes `crontab -l` / `crontab -`.

Marked `@pytest.mark.release` because it mutates the ambient user crontab
and requires the `crontab` binary, which is not present in the fast test
sandbox.
"""

import os
import subprocess

import pytest

from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_schedule.implementations.local.crontab import build_marker_comment
from imbue.mngr_schedule.testing import REPO_ROOT
from imbue.mngr_schedule.testing import build_disable_plugin_args
from imbue.mngr_schedule.testing import build_subprocess_env
from imbue.mngr_schedule.testing import remove_test_trigger

_ENABLED_PLUGINS = frozenset({"schedule"})


def _read_crontab() -> str:
    """Read the current user's crontab, returning '' when unset or missing.

    Returns '' in two cases:
      - The `crontab` binary is not installed (FileNotFoundError from exec).
      - The binary exits non-zero (covers 'no crontab for <user>' and
        similar cases where there simply isn't an entry to observe).
    """
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


@pytest.mark.release
@pytest.mark.timeout(300)
def test_schedule_local_add_and_remove_lifecycle() -> None:
    """End-to-end: CLI add installs a real crontab entry; CLI remove takes it out."""
    # Unique per test run to avoid colliding with other workers or leftover state.
    trigger_name = f"test-local-lifecycle-{os.getpid()}-{get_short_random_string()}"
    env = build_subprocess_env()
    # `build_subprocess_env` sets `MNGR_PREFIX` directly, which overrides the
    # default prefix the CLI would derive from `MNGR_ROOT_NAME`. The CLI writes
    # the marker using the effective prefix, so derive the expected marker from
    # the same env var. The full marker (prefix + "schedule:" + name) is matched
    # rather than the bare trigger name so parallel xdist workers don't
    # false-positive on each other's entries in the shared user crontab.
    marker = build_marker_comment(env["MNGR_PREFIX"], trigger_name)
    disable_args = build_disable_plugin_args(_ENABLED_PLUGINS)

    try:
        add_result = subprocess.run(
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
                "--type headless_command --foreground"
                " -S agent_types.headless_command.command='echo hello-from-local-lifecycle'",
                "--schedule",
                "0 3 * * *",
                "--provider",
                "local",
                "--no-auto-merge",
                *disable_args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=REPO_ROOT,
        )
        assert add_result.returncode == 0, (
            f"schedule add failed\nstdout: {add_result.stdout}\nstderr: {add_result.stderr}"
        )
        assert marker in _read_crontab(), (
            f"Expected marker '{marker}' in crontab after add; crontab contents:\n{_read_crontab()}"
        )

        remove_result = subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "schedule",
                "remove",
                trigger_name,
                "--provider",
                "local",
                "--force",
                *disable_args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert remove_result.returncode == 0, (
            f"schedule remove failed\nstdout: {remove_result.stdout}\nstderr: {remove_result.stderr}"
        )
        assert marker not in _read_crontab(), (
            f"Marker '{marker}' still in crontab after remove; crontab contents:\n{_read_crontab()}"
        )
    finally:
        # Best-effort cleanup: if any assert above fired between add and remove,
        # the marker is still in the user's crontab. Call the CLI again so we
        # don't strand state on the host running the release suite.
        remove_test_trigger(trigger_name, env, _ENABLED_PLUGINS, provider="local")
