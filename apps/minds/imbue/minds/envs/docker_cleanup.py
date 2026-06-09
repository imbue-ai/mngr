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
    cg = parent_concurrency_group.make_concurrency_group(name=f"docker-cleanup-{container_name}")
    with cg:
        # Probe for the container. This also distinguishes "no docker daemon"
        # (skip) from "container already gone" (success).
        try:
            inspect_result = cg.run_process_to_completion(
                command=["docker", "container", "inspect", container_name],
                timeout=_DOCKER_CMD_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=dict(os.environ),
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
        remove_result = cg.run_process_to_completion(
            command=["docker", "rm", "-f", container_name],
            timeout=_DOCKER_CMD_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=dict(os.environ),
        )
        if remove_result.returncode != 0:
            stderr = remove_result.stderr.strip() or remove_result.stdout.strip()
            raise DockerCleanupError(
                f"`docker rm -f {container_name}` failed (exit {remove_result.returncode}): {stderr}"
            )
        logger.info("Removed Docker state container {}", container_name)

        # The state volume has the same name as the container; remove it now
        # that the container (its only mount) is gone. Absent volume = success.
        volume_result = cg.run_process_to_completion(
            command=["docker", "volume", "rm", container_name],
            timeout=_DOCKER_CMD_TIMEOUT_SECONDS,
            is_checked_after=False,
            env=dict(os.environ),
        )
        if volume_result.returncode == 0:
            logger.info("Removed Docker state volume {}", container_name)
            return
        combined = (volume_result.stderr + volume_result.stdout).lower()
        if "no such volume" in combined or "not found" in combined:
            logger.debug("No Docker state volume {} to remove", container_name)
            return
        stderr = volume_result.stderr.strip() or volume_result.stdout.strip()
        raise DockerCleanupError(
            f"`docker volume rm {container_name}` failed (exit {volume_result.returncode}): {stderr}"
        )


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
    cg = parent_concurrency_group.make_concurrency_group(name=f"docker-stop-state-{container_name}")
    with cg:
        try:
            stop_result = cg.run_process_to_completion(
                command=["docker", "stop", container_name],
                timeout=_DOCKER_CMD_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=dict(os.environ),
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
