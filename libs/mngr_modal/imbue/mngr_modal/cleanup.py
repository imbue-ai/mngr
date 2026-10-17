"""Age-based reaper for the Modal test environments that test runs leave behind.

Driven by ``scripts/cleanup_old_modal_test_environments.py`` (a CI job on every
push to main and pull request) and by the session-scoped fixtures in
``conftest.py``. Analogous to ``imbue.mngr_vultr.cleanup`` for Vultr.
"""

import json
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.env_utils import TEST_ENV_PATTERN
from imbue.mngr.utils.env_utils import TEST_ENV_PREFIX
from imbue.mngr.utils.testing import ModalCleanupOutcome
from imbue.mngr_modal.modal_cli import parse_modal_app_listings
from imbue.mngr_modal.modal_cli import parse_modal_volume_listings

_MODAL_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0


def _run_modal(label: str, args: Sequence[str]) -> FinishedProcess:
    """Run ``uv run modal <args>`` to completion in its own ConcurrencyGroup.

    Unchecked because every caller reads ``returncode`` itself; that also keeps a
    timeout as data on the result rather than an exception, which suits callers
    that must never raise. A launch failure still raises ProcessSetupError.
    """
    with ConcurrencyGroup(name=f"modal-{label}") as cg:
        return cg.run_process_to_completion(
            ["uv", "run", "modal", *args],
            is_checked_after=False,
            timeout=_MODAL_COMMAND_TIMEOUT_SECONDS,
        )


def _failure_detail(result: FinishedProcess) -> str:
    """Explain a failed modal invocation, since a timeout leaves both streams empty."""
    if result.is_timed_out:
        return f"timed out after {_MODAL_COMMAND_TIMEOUT_SECONDS}s"
    return (result.stderr or result.stdout).strip()


def _parse_test_env_timestamp(env_name: str) -> datetime | None:
    """Parse the timestamp from a test environment name.

    Returns the datetime if the name matches the test environment pattern,
    otherwise returns None.
    """
    match = TEST_ENV_PATTERN.match(env_name)
    if not match:
        return None

    year, month, day, hour, minute, second = match.groups()
    return datetime(
        int(year),
        int(month),
        int(day),
        int(hour),
        int(minute),
        int(second),
        tzinfo=timezone.utc,
    )


def list_modal_test_environments() -> list[str]:
    """List all Modal test environments.

    Returns a list of environment names that match the test environment pattern
    (mngr_test-YYYY-MM-DD-HH-MM-SS*).
    """
    try:
        result = _run_modal("environment-list", ["environment", "list", "--json"])
        if result.returncode != 0:
            logger.warning("Failed to list Modal environments: {}", _failure_detail(result))
            return []

        environments = json.loads(result.stdout)
        test_envs: list[str] = []

        for env in environments:
            env_name = env.get("name", "")
            if env_name.startswith(TEST_ENV_PREFIX):
                test_envs.append(env_name)

        return test_envs
    except (ProcessError, json.JSONDecodeError) as e:
        logger.warning("Error listing Modal environments: {}", e)
        return []


def find_old_test_environments(
    max_age: timedelta,
) -> list[str]:
    """Find Modal test environments older than the specified age.

    Returns a list of environment names that are older than max_age.
    The age is determined by parsing the timestamp from the environment name.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - max_age
    old_envs: list[str] = []

    for env_name in list_modal_test_environments():
        timestamp = _parse_test_env_timestamp(env_name)
        if timestamp is not None and timestamp < cutoff:
            old_envs.append(env_name)

    return old_envs


def delete_modal_apps_in_environment(environment_name: str) -> None:
    """Stop all Modal apps in the specified environment.

    This is robust to concurrent deletion - failures result in warnings, not errors.
    """
    try:
        result = _run_modal(f"app-list-{environment_name}", ["app", "list", "--env", environment_name, "--json"])
        if result.returncode != 0:
            # Environment may not exist or may have been deleted concurrently
            logger.warning("Failed to list apps in environment {}: {}", environment_name, _failure_detail(result))
            return

        apps = parse_modal_app_listings(json.loads(result.stdout))
        for app in apps:
            try:
                stop_result = _run_modal(
                    f"app-stop-{app.app_id}",
                    # --env: `modal app stop` resolves the id in the *default* environment
                    # when unscoped, where a test environment's apps are not visible, so
                    # every stop would fail with "No App with ID ... found".
                    # --yes: skip the interactive confirmation, which otherwise aborts the
                    # stop in non-interactive runs (CI / release tests) so the app is never
                    # stopped and only the environment deletion reaps it.
                    ["app", "stop", app.app_id, "--env", environment_name, "--yes"],
                )
                if stop_result.returncode != 0:
                    logger.warning(
                        "Modal app stop returned non-zero for {} ({}): {}",
                        app.description,
                        app.app_id,
                        _failure_detail(stop_result),
                    )
                else:
                    logger.debug("Stopped Modal app {} ({})", app.description, app.app_id)
            except ProcessError as e:
                logger.warning("Failed to stop Modal app {} ({}): {}", app.description, app.app_id, e)
    except (ProcessError, json.JSONDecodeError) as e:
        logger.warning("Failed to list/delete Modal apps in environment {}: {}", environment_name, e)


def delete_modal_volumes_in_environment(environment_name: str) -> None:
    """Delete all Modal volumes in the specified environment.

    This is robust to concurrent deletion - failures result in warnings, not errors.
    """
    try:
        result = _run_modal(f"volume-list-{environment_name}", ["volume", "list", "--env", environment_name, "--json"])
        if result.returncode != 0:
            # Environment may not exist or may have been deleted concurrently
            logger.warning("Failed to list volumes in environment {}: {}", environment_name, _failure_detail(result))
            return

        volumes = parse_modal_volume_listings(json.loads(result.stdout))
        for volume in volumes:
            try:
                del_result = _run_modal(
                    f"volume-delete-{volume.name}",
                    ["volume", "delete", volume.name, "--env", environment_name, "--yes"],
                )
                if del_result.returncode != 0:
                    logger.warning(
                        "Modal volume delete returned non-zero for {} in env {}: {}",
                        volume.name,
                        environment_name,
                        _failure_detail(del_result),
                    )
                else:
                    logger.debug("Deleted Modal volume {} in environment {}", volume.name, environment_name)
            except ProcessError as e:
                logger.warning(
                    "Failed to delete Modal volume {} in environment {}: {}", volume.name, environment_name, e
                )
    except (ProcessError, json.JSONDecodeError) as e:
        logger.warning("Failed to list/delete Modal volumes in environment {}: {}", environment_name, e)


def delete_modal_environment(environment_name: str) -> ModalCleanupOutcome:
    """Delete a Modal environment.

    Robust to concurrent deletion: returns a ModalCleanupOutcome instead of
    raising. Callers should treat DELETED and NOT_FOUND as success (the env
    is gone, whether we did the deleting or someone else did) and only
    FAILED as a reason to keep the env tracked for leak detection.
    """
    try:
        result = _run_modal(
            f"environment-delete-{environment_name}", ["environment", "delete", environment_name, "--yes"]
        )
        if result.returncode == 0:
            logger.debug("Deleted Modal environment {}", environment_name)
            return ModalCleanupOutcome.DELETED
        stderr = _failure_detail(result)
        # Require the env name to appear alongside "not found" so unrelated
        # errors (e.g. "credentials not found", "config file not found") do
        # NOT get misclassified as NOT_FOUND. NOT_FOUND lets the caller drop
        # the env from leak tracking; misclassifying a real FAILED here would
        # silently defeat the session-end leak detector for that env.
        stderr_lower = stderr.lower()
        if "not found" in stderr_lower and environment_name.lower() in stderr_lower:
            logger.debug("Modal environment {} already gone: {}", environment_name, stderr.strip())
            return ModalCleanupOutcome.NOT_FOUND
        logger.warning(
            "Modal environment delete returned non-zero for {}: {}",
            environment_name,
            stderr,
        )
        return ModalCleanupOutcome.FAILED
    except ProcessError as e:
        logger.warning("Failed to delete Modal environment {}: {}", environment_name, e)
        return ModalCleanupOutcome.FAILED


class ModalTestEnvironmentSweepResult(FrozenModel):
    """What one run of the old-test-environment sweep achieved."""

    deleted_environment_names: tuple[str, ...] = Field(
        description="Environments that are now gone, whether this run deleted them or found them already absent"
    )
    failed_environment_names: tuple[str, ...] = Field(
        description="Environments whose delete failed, which therefore remain on the Modal account"
    )


def sweep_old_modal_test_environment(environment_name: str) -> ModalCleanupOutcome:
    """Empty one old test environment and delete it, returning the environment-level outcome."""
    delete_modal_apps_in_environment(environment_name)
    delete_modal_volumes_in_environment(environment_name)
    return delete_modal_environment(environment_name)


def cleanup_old_modal_test_environments(
    max_age_hours: float = 1.0,
    *,
    find_old_environments: Callable[[timedelta], list[str]] = find_old_test_environments,
    sweep_environment: Callable[[str], ModalCleanupOutcome] = sweep_old_modal_test_environment,
) -> ModalTestEnvironmentSweepResult:
    """Clean up Modal test environments older than the specified age.

    This function finds all Modal test environments with names matching the pattern
    mngr_test-YYYY-MM-DD-HH-MM-SS*, parses the timestamp from the name, and deletes
    those that are older than max_age_hours.

    This function is designed to be robust to concurrent deletion: it never raises
    on a failed delete, so the loop always processes every old environment. App
    and volume failures log at warning level. A FAILED environment delete logs at
    error level so a stuck safety-net run is greppable in the CI logs that drive
    this script.

    Warning vs error rationale: `modal environment delete` cascades and "deletes
    all apps in the selected environment" (per Modal CLI docs), so individual
    app/volume delete failures are best-effort and not real leaks as long as the
    env-level delete succeeds. Reserving error level for the env-level FAILED
    keeps CI logs free of false-positive noise.

    The collaborators are injected so the sweep can be unit-tested without
    invoking the real Modal CLI.
    """
    max_age = timedelta(hours=max_age_hours)
    old_envs = find_old_environments(max_age)

    if old_envs:
        logger.info("Found {} old Modal test environments to clean up", len(old_envs))
    else:
        logger.info("No old Modal test environments found (older than {} hours)", max_age_hours)

    deleted: list[str] = []
    failed: list[str] = []
    for env_name in old_envs:
        logger.info("Cleaning up old test environment: {}", env_name)
        outcome = sweep_environment(env_name)
        match outcome:
            case ModalCleanupOutcome.DELETED | ModalCleanupOutcome.NOT_FOUND:
                deleted.append(env_name)
            case ModalCleanupOutcome.FAILED:
                failed.append(env_name)
                logger.error(
                    "Safety-net cleanup failed to delete Modal environment {}; leaving it for the next cleanup run.",
                    env_name,
                )
            case _ as unreachable:
                assert_never(unreachable)

    return ModalTestEnvironmentSweepResult(
        deleted_environment_names=tuple(deleted), failed_environment_names=tuple(failed)
    )
