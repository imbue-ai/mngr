"""Remove a minds env's mngr Docker state container + backing volume.

The mngr docker provider keeps a singleton *state container* per
``(MNGR_PREFIX, user_id)`` -- named ``<MNGR_PREFIX>docker-state-<user_id>``
-- that holds host records / agent data for every docker host in that
profile. It runs with ``restart_policy=unless-stopped`` and is never
removed by ``mngr destroy`` (which only tears down individual hosts).
Each minds env has its own ``MNGR_PREFIX``, so resetting or destroying
an env abandons that env's state container, which then runs forever.

This module removes the ONE exact container/volume for the env being
torn down. It never matches by prefix or label: over-matching would
destroy unrelated state.

Pure subprocess-based (shells out to ``docker``) to avoid pulling
``imbue.mngr`` into this path. The mngr conventions (the state-container
name shape and the ``user_id`` filename) are intentionally inlined here
rather than imported, so a drift in mngr surfaces as a test failure
instead of silently following along.
"""

import os
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.bootstrap import read_active_profile_dir
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.bootstrap import root_name_for_env_name
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

# Mirrors imbue.mngr.config.data_types.USER_ID_FILENAME. Inlined so this
# module stays free of any imbue.mngr import; see the module docstring.
_USER_ID_FILENAME: Final[str] = "user_id"

_DOCKER_CMD_TIMEOUT_SECONDS: Final[float] = 60.0


class DockerCleanupError(MindError):
    """Raised when a present Docker state container / volume cannot be removed."""


def read_profile_user_id(mngr_host_dir: Path) -> str | None:
    """Read the active mngr profile's ``user_id``, or None if it can't be resolved.

    The active profile dir is resolved via
    ``bootstrap.read_active_profile_dir`` and the user id lives at
    ``<profile_dir>/user_id``.
    """
    profile_dir = read_active_profile_dir(mngr_host_dir)
    if profile_dir is None:
        return None
    user_id_path = profile_dir / _USER_ID_FILENAME
    if not user_id_path.is_file():
        return None
    try:
        return user_id_path.read_text().strip() or None
    except OSError as e:
        logger.warning("Could not read mngr profile user_id {}: {}", user_id_path, e)
        return None


def state_container_name(mngr_prefix: str, user_id: str) -> str:
    """The docker state container name (its backing volume shares the name)."""
    return f"{mngr_prefix}docker-state-{user_id}"


def _is_docker_daemon_unavailable(combined_output: str) -> bool:
    """Whether ``docker``'s output indicates the daemon is unreachable (vs a real error)."""
    lowered = combined_output.lower()
    return (
        "cannot connect to the docker daemon" in lowered
        or "is the docker daemon running" in lowered
        or "error during connect" in lowered
    )


def _run_docker_command(
    command: list[str],
    *,
    parent_concurrency_group: ConcurrencyGroup,
    scope_name: str,
) -> FinishedProcess:
    """Run one ``docker`` subprocess to completion in its own CG scope, returning the result *after* the CG exits.

    Mirrors :func:`imbue.minds.envs.vault_reader._run_vault_command`: returning the
    result only after the concurrency group's ``__exit__`` lets callers classify
    the output and raise plain :class:`DockerCleanupError` domain exceptions
    *without* them being re-wrapped in a ``ConcurrencyExceptionGroup`` by the CG.
    A raise from inside the ``with cg`` block would escape as that group, which
    callers' ``except DockerCleanupError`` could not catch.

    A :class:`ProcessSetupError` (the ``docker`` binary could not be launched) is
    likewise re-raised outside the CG scope, so callers catch it directly as a
    CLI-absent no-op rather than as a wrapped group.
    """
    cg = parent_concurrency_group.make_concurrency_group(name=scope_name)
    setup_error: ProcessSetupError | None = None
    result: FinishedProcess | None = None
    with cg:
        try:
            result = cg.run_process_to_completion(
                command=command,
                timeout=_DOCKER_CMD_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=dict(os.environ),
            )
        except ProcessSetupError as e:
            setup_error = e
    if setup_error is not None:
        raise setup_error
    assert result is not None
    return result


def remove_state_container(
    *,
    container_name: str,
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    """Remove the exact ``container_name`` state container and its backing volume.

    Best-effort about Docker's *presence*: when the ``docker`` CLI is
    missing or its daemon is unreachable (Modal- / imbue_cloud-only
    envs), this is a silent no-op. A container that is already absent is
    a success. But when the container is present and ``docker rm`` fails,
    this raises :class:`DockerCleanupError` so a real failure surfaces.
    """
    # Probe for the container. This also distinguishes "no docker daemon"
    # (skip) from "container already gone" (success).
    try:
        inspect_result = _run_docker_command(
            ["docker", "container", "inspect", container_name],
            parent_concurrency_group=parent_concurrency_group,
            scope_name=f"docker-cleanup-{container_name}",
        )
    except ProcessSetupError as e:
        # The docker binary is not installed / could not be launched.
        logger.info("Docker CLI unavailable; skipping state-container cleanup for {} ({})", container_name, e)
        return

    if inspect_result.returncode != 0:
        combined = inspect_result.stderr + inspect_result.stdout
        if _is_docker_daemon_unavailable(combined):
            logger.info("Docker daemon unavailable; skipping state-container cleanup for {}", container_name)
            return
        logger.debug("No Docker state container {} to remove", container_name)
        return

    # Container exists -- remove it, then its backing named volume.
    remove_result = _run_docker_command(
        ["docker", "rm", "-f", container_name],
        parent_concurrency_group=parent_concurrency_group,
        scope_name=f"docker-cleanup-rm-{container_name}",
    )
    if remove_result.returncode != 0:
        stderr = remove_result.stderr.strip() or remove_result.stdout.strip()
        raise DockerCleanupError(f"`docker rm -f {container_name}` failed (exit {remove_result.returncode}): {stderr}")
    logger.info("Removed Docker state container {}", container_name)

    # The state volume has the same name as the container; remove it now
    # that the container (its only mount) is gone. Absent volume = success.
    volume_result = _run_docker_command(
        ["docker", "volume", "rm", container_name],
        parent_concurrency_group=parent_concurrency_group,
        scope_name=f"docker-cleanup-vol-{container_name}",
    )
    if volume_result.returncode == 0:
        logger.info("Removed Docker state volume {}", container_name)
        return
    combined = (volume_result.stderr + volume_result.stdout).lower()
    if "no such volume" in combined or "not found" in combined:
        logger.debug("No Docker state volume {} to remove", container_name)
        return
    stderr = volume_result.stderr.strip() or volume_result.stdout.strip()
    raise DockerCleanupError(f"`docker volume rm {container_name}` failed (exit {volume_result.returncode}): {stderr}")


def stop_state_container(
    *,
    container_name: str,
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    """Stop (not remove) the exact ``container_name`` state container, preserving its volume.

    Unlike :func:`remove_state_container`, this keeps the backing volume (and
    thus the host records / agent data) intact: it just frees the running
    container. The mngr docker provider re-creates / restarts the state
    container on its next operation, so stopping it is safe and reversible --
    used at app quit to free local resources after the workspaces are stopped.

    Best-effort about Docker's *presence*: a missing ``docker`` CLI or an
    unreachable daemon is a silent no-op, and a container that is absent or
    already stopped is a success. A present, running container that fails to
    stop raises :class:`DockerCleanupError`.
    """
    try:
        stop_result = _run_docker_command(
            ["docker", "stop", container_name],
            parent_concurrency_group=parent_concurrency_group,
            scope_name=f"docker-stop-state-{container_name}",
        )
    except ProcessSetupError as e:
        logger.info("Docker CLI unavailable; skipping state-container stop for {} ({})", container_name, e)
        return

    if stop_result.returncode == 0:
        logger.info("Stopped Docker state container {}", container_name)
        return

    # ``docker stop`` of an absent container exits nonzero; distinguish that
    # (and a down daemon) from a real failure to stop a present container.
    combined = stop_result.stderr + stop_result.stdout
    if _is_docker_daemon_unavailable(combined):
        logger.info("Docker daemon unavailable; skipping state-container stop for {}", container_name)
        return
    lowered = combined.lower()
    if "no such container" in lowered or "not found" in lowered:
        logger.debug("No Docker state container {} to stop", container_name)
        return
    stderr = stop_result.stderr.strip() or stop_result.stdout.strip()
    raise DockerCleanupError(f"`docker stop {container_name}` failed (exit {stop_result.returncode}): {stderr}")


def stop_active_env_state_container(
    *,
    mngr_host_dir: Path,
    parent_concurrency_group: ConcurrencyGroup,
) -> bool:
    """Stop the *running* env's mngr Docker state container (preserving its volume).

    Resolves the active ``MINDS_ROOT_NAME`` -> ``MNGR_PREFIX`` and the mngr
    profile's ``user_id`` to target the one exact container by name (never by
    prefix or label). Returns True when a container name was resolved and a stop
    was attempted, False when ``user_id`` could not be resolved (nothing
    targeted). Scoped to this env's prefix, so a differently-prefixed state
    container (e.g. the user's own ``mngr-`` docker usage) is never touched.
    """
    root_name = resolve_minds_root_name()
    mngr_prefix = mngr_prefix_for(root_name)
    user_id = read_profile_user_id(mngr_host_dir)
    if user_id is None:
        logger.warning(
            "Could not resolve mngr profile user_id under {}; skipping Docker state-container stop.", mngr_host_dir
        )
        return False
    container_name = state_container_name(mngr_prefix, user_id)
    stop_state_container(container_name=container_name, parent_concurrency_group=parent_concurrency_group)
    return True


def start_state_container(
    *,
    container_name: str,
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    """Start the exact ``container_name`` state container if present and stopped.

    The inverse of :func:`stop_state_container`: used at app launch to bring the
    docker state container back up *before* discovery runs. ``stop_state_container``
    stops it at quit; without restarting it here, read-only discovery would find
    it stopped, fail every host-record read, and report zero hosts -- dropping
    every workspace on the first snapshot. ``docker start`` of an already-running
    container is a harmless success.

    Best-effort about Docker's *presence*: a missing ``docker`` CLI or an
    unreachable daemon is a silent no-op, and an absent container (never created)
    is a success -- it is created lazily on the first ``mngr create``. A present
    container that fails to start raises :class:`DockerCleanupError`.
    """
    try:
        start_result = _run_docker_command(
            ["docker", "start", container_name],
            parent_concurrency_group=parent_concurrency_group,
            scope_name=f"docker-start-state-{container_name}",
        )
    except ProcessSetupError as e:
        logger.info("Docker CLI unavailable; skipping state-container start for {} ({})", container_name, e)
        return

    if start_result.returncode == 0:
        logger.info("Started Docker state container {}", container_name)
        return

    combined = start_result.stderr + start_result.stdout
    if _is_docker_daemon_unavailable(combined):
        logger.info("Docker daemon unavailable; skipping state-container start for {}", container_name)
        return
    lowered = combined.lower()
    if "no such container" in lowered or "not found" in lowered:
        logger.debug("No Docker state container {} to start (created lazily on first use)", container_name)
        return
    stderr = start_result.stderr.strip() or start_result.stdout.strip()
    raise DockerCleanupError(f"`docker start {container_name}` failed (exit {start_result.returncode}): {stderr}")


def start_active_env_state_container(
    *,
    mngr_host_dir: Path,
    parent_concurrency_group: ConcurrencyGroup,
) -> bool:
    """Start the *running* env's mngr Docker state container if it is stopped.

    Inverse of :func:`stop_active_env_state_container`. Resolves the active
    ``MINDS_ROOT_NAME`` -> ``MNGR_PREFIX`` and the mngr profile's ``user_id`` to
    target the one exact container by name. Returns True when a container name
    was resolved and a start was attempted, False when ``user_id`` could not be
    resolved (nothing targeted). Scoped to this env's prefix, so a
    differently-prefixed state container is never touched.
    """
    root_name = resolve_minds_root_name()
    mngr_prefix = mngr_prefix_for(root_name)
    user_id = read_profile_user_id(mngr_host_dir)
    if user_id is None:
        logger.warning(
            "Could not resolve mngr profile user_id under {}; skipping Docker state-container start.", mngr_host_dir
        )
        return False
    container_name = state_container_name(mngr_prefix, user_id)
    start_state_container(container_name=container_name, parent_concurrency_group=parent_concurrency_group)
    return True


def cleanup_env_state_container(
    name: DevEnvName,
    *,
    parent_concurrency_group: ConcurrencyGroup,
) -> None:
    """Remove the env's mngr Docker state container + volume, targeting it exactly.

    Resolves the env's ``MNGR_PREFIX`` and the mngr profile's ``user_id``
    to build the exact container name. When ``user_id`` can't be resolved
    (e.g. the env root was already removed), skips with a warning rather
    than matching anything broader.
    """
    root_name = root_name_for_env_name(str(name))
    mngr_host_dir = mngr_host_dir_for(root_name)
    mngr_prefix = mngr_prefix_for(root_name)

    user_id = read_profile_user_id(mngr_host_dir)
    if user_id is None:
        logger.warning(
            "Could not resolve mngr profile user_id under {} for env {!r}; skipping Docker "
            "state-container cleanup (cannot target the exact container by name).",
            mngr_host_dir,
            str(name),
        )
        return

    container_name = state_container_name(mngr_prefix, user_id)
    remove_state_container(container_name=container_name, parent_concurrency_group=parent_concurrency_group)
