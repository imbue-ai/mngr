import hashlib
import json
import subprocess
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
from imbue.mngr.providers.docker.instance import create_docker_client
from imbue.mngr.providers.docker.volume import LABEL_PROVIDER
from imbue.mngr.providers.docker.volume import state_volume_name
from imbue.mngr.providers.local.volume import LocalVolume
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import worker_docker_state_prefixes


def write_fake_docker_context(config_dir: Path, context_name: str, host_url: str) -> None:
    """Write a fake Docker config and context metadata into *config_dir*.

    Used by the ``fake_docker_config`` fixture to set up a deterministic
    Docker context for tests that exercise ``_get_docker_context_host``.
    """
    (config_dir / "config.json").write_text(json.dumps({"currentContext": context_name}))
    if context_name == "default":
        return
    ctx_id = hashlib.sha256(context_name.encode()).hexdigest()
    meta_dir = config_dir / "contexts" / "meta" / ctx_id
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "Name": context_name,
        "Metadata": {},
        "Endpoints": {"docker": {"Host": host_url, "SkipTLSVerify": False}},
    }
    (meta_dir / "meta.json").write_text(json.dumps(meta))


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
        client = create_docker_client()
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


def remove_all_containers_by_prefix_via_cli(prefix: str) -> None:
    """Force-remove all Docker containers and volumes whose name starts with *prefix* via the docker CLI.

    Used by subprocess-based test fixtures whose teardown runs while the
    resource guard is active (the guard keeps ``_PYTEST_GUARD_PHASE`` at
    "call" through teardown). Such tests are marked ``docker`` (so the docker
    CLI is permitted) but not ``docker_sdk``, which means the SDK-based
    ``remove_all_containers_by_prefix`` would be blocked by the guard and
    silently fail -- leaking the singleton state container. The docker CLI is
    permitted, so this variant cleans up reliably. Matching is by name prefix
    (the per-test prefix is unique), covering both host and state containers.

    Errors are ignored so cleanup proceeds on a best-effort basis.
    """

    def _docker(*args: str) -> str:
        try:
            result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            return ""
        return result.stdout if result.returncode == 0 else ""

    # Remove matching containers (host + state) first so their volumes are free.
    container_ids = [
        line.split("\t", 1)[0]
        for line in _docker("ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}").splitlines()
        if "\t" in line and line.split("\t", 1)[1].startswith(prefix)
    ]
    if container_ids:
        _docker("rm", "-f", *container_ids)

    # The state container's backing volume shares the container's name.
    volume_names = [
        name for name in _docker("volume", "ls", "--format", "{{.Name}}").splitlines() if name.startswith(prefix)
    ]
    if volume_names:
        _docker("volume", "rm", "-f", *volume_names)


def make_docker_provider(mngr_ctx: MngrContext, name: str = "test-docker") -> DockerProviderInstance:
    # Explicitly pin isolate_host_volumes=False so the autouse loguru-warning
    # guard does not trip on the deprecation warning emitted for the None
    # (unset) default. Tests that specifically need to exercise None should
    # construct DockerProviderConfig themselves under capture_loguru().
    config = DockerProviderConfig(isolate_host_volumes=False)
    return DockerProviderInstance(
        name=ProviderInstanceName(name),
        host_dir=Path("/mngr"),
        mngr_ctx=mngr_ctx,
        config=config,
    )


def make_offline_docker_provider(mngr_ctx: MngrContext, name: str = "test-docker-offline") -> DockerProviderInstance:
    """Create a Docker provider that points to a non-existent Docker socket.

    Useful for testing graceful degradation when the Docker daemon is unavailable.
    """
    config = DockerProviderConfig(host="unix:///nonexistent/docker.sock", isolate_host_volumes=False)
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
    isolate_host_volumes: bool = False,
) -> Generator[DockerProviderInstance, None, None]:
    """Create a Docker provider with a unique name and clean up all hosts on teardown.

    ``isolate_host_volumes`` is passed through to the provider config so callers
    can exercise the isolated (volume-subpath) mount path without having to
    reimplement the cleanup logic.
    """
    unique_name = f"docker-test-{get_short_random_string()}"
    config = DockerProviderConfig(isolate_host_volumes=isolate_host_volumes)
    provider = DockerProviderInstance(
        name=ProviderInstanceName(unique_name),
        host_dir=Path("/mngr"),
        mngr_ctx=mngr_ctx,
        config=config,
    )
    # Register the prefix so the session-end safety net can attribute any
    # leaked state container (named "<prefix>docker-state-<user_id>") to this
    # worker and fail the suite if our cleanup below fails to remove it.
    worker_docker_state_prefixes.append(mngr_ctx.config.prefix)
    yield provider

    try:
        cg = mngr_ctx.concurrency_group
        discovered = provider.discover_hosts(cg, include_destroyed=True)
        for host in discovered:
            try:
                provider.destroy_host(host.host_id)
            except (MngrError, docker.errors.DockerException, OSError):
                pass
            try:
                provider.delete_host(provider.get_host(host.host_id))
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
