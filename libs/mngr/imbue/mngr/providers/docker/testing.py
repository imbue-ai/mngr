from collections.abc import Generator
from pathlib import Path

import docker
import docker.errors
import docker.models.containers

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.docker.config import DockerProviderConfig
from imbue.mngr.providers.docker.instance import DockerProviderInstance
from imbue.mngr.providers.docker.volume import LABEL_PROVIDER
from imbue.mngr.providers.docker.volume import state_volume_name
from imbue.mngr.providers.local.volume import LocalVolume
from imbue.mngr.utils.testing import get_short_random_string


def remove_docker_container_and_volume(
    client: docker.DockerClient,
    container: docker.models.containers.Container,
) -> None:
    """Remove a Docker container and its backing volume (if any).

    The state container's backing Docker volume has the same name as the
    container.  The container must be removed first because Docker refuses
    to remove volumes that are still mounted.

    Errors are silently ignored so that cleanup proceeds on a best-effort
    basis.
    """
    name = container.name or ""
    try:
        container.remove(force=True)
    except docker.errors.DockerException:
        pass
    if name:
        try:
            client.volumes.get(name).remove(force=True)
        except (docker.errors.NotFound, docker.errors.DockerException):
            pass


def remove_all_containers_by_prefix(
    prefix: str,
    provider_name: str,
) -> None:
    """Remove ALL Docker containers whose name starts with *prefix*.

    Finds containers by ``LABEL_PROVIDER=provider_name`` and then filters
    by name prefix.  This catches containers that ``_list_containers``
    might miss (e.g. if the provider name differs from what was used at
    creation time, or if the test was interrupted before normal cleanup).

    Creates and closes its own Docker client so callers don't need one.
    """
    try:
        client = docker.from_env()
    except (docker.errors.DockerException, OSError):
        return

    try:
        containers = client.containers.list(
            all=True,
            filters={"label": [f"{LABEL_PROVIDER}={provider_name}"]},
        )
        for container in containers:
            name = container.name or ""
            if name.startswith(prefix):
                remove_docker_container_and_volume(client, container)
    except (docker.errors.DockerException, OSError):
        pass
    finally:
        client.close()


def make_docker_provider(mngr_ctx: MngrContext, name: str = "test-docker") -> DockerProviderInstance:
    config = DockerProviderConfig()
    return DockerProviderInstance(
        name=ProviderInstanceName(name),
        host_dir=Path("/mngr"),
        mngr_ctx=mngr_ctx,
        config=config,
    )


def make_docker_provider_with_local_volume(
    mngr_ctx: MngrContext,
    volume_root: Path,
) -> DockerProviderInstance:
    """Create a Docker provider using a LocalVolume instead of a real Docker volume.

    This avoids needing a running Docker daemon for tests that only exercise
    state-volume logic (list_volumes, delete_volume, host store, etc.).
    """
    provider = make_docker_provider(mngr_ctx)
    provider.__dict__["_state_volume"] = LocalVolume(root_path=volume_root)
    return provider


def make_docker_provider_with_cleanup(
    mngr_ctx: MngrContext,
) -> Generator[DockerProviderInstance, None, None]:
    """Create a Docker provider with a unique name and clean up all hosts on teardown."""
    unique_name = f"docker-test-{get_short_random_string()}"
    provider = make_docker_provider(mngr_ctx, unique_name)
    yield provider

    try:
        cg = mngr_ctx.concurrency_group
        discovered = provider.discover_hosts(cg, include_destroyed=True)
        for host in discovered:
            try:
                provider.destroy_host(host.host_id, delete_snapshots=True)
            except (MngrError, docker.errors.DockerException, OSError):
                pass
    except (MngrError, docker.errors.DockerException, OSError):
        pass

    try:
        for container in provider._list_containers():
            remove_docker_container_and_volume(provider._docker_client, container)
    except (MngrError, docker.errors.DockerException):
        pass

    # Also clean up by prefix in case _list_containers missed containers
    # due to a prefix mismatch.
    remove_all_containers_by_prefix(mngr_ctx.config.prefix, unique_name)

    # Remove the Docker named volume backing the state container (in case
    # the state container was already removed above but the volume was not).
    try:
        user_id = str(mngr_ctx.get_profile_user_id())
        prefix = mngr_ctx.config.prefix
        vol_name = state_volume_name(prefix, user_id)
        provider._docker_client.volumes.get(vol_name).remove(force=True)
    except (docker.errors.NotFound, docker.errors.DockerException, OSError, MngrError):
        pass

    try:
        provider.close()
    except (OSError, docker.errors.DockerException):
        pass
