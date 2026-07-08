from functools import cache
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import model_validator

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import DockerConfigValidationError
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName


@cache
def _emit_isolate_default_warning_once() -> None:
    """Emit the `isolate_host_volumes` default-flip deprecation warning at most once per process.

    Dedup is provided by ``functools.cache``: the body runs on the first call
    and is a no-op on every subsequent call until ``cache_clear()`` is invoked
    (which tests do between cases to re-exercise the emission path).
    """
    logger.warning(
        "Docker provider config `isolate_host_volumes` is unset. The default will change "
        "to True in a future release, which will cause each host container to see only its "
        "own host_dir sub-folder instead of the entire shared state volume. To keep the "
        "current (shared) behavior, set isolate_host_volumes=false explicitly. To opt in "
        "to the new behavior now, set isolate_host_volumes=true (requires Docker Engine >= 25.0)."
    )


class DockerProviderConfig(ProviderInstanceConfig):
    """Configuration for the docker provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("docker"),
        description="Provider backend (always 'docker' for this type)",
    )
    host: str = Field(
        default="",
        description=(
            "Docker host URL (e.g., 'ssh://user@server', 'tcp://host:2376'). Empty string means local Docker daemon."
        ),
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data inside containers (defaults to /mngr)",
    )
    default_image: str | None = Field(
        default=None,
        description="Default base image. None uses debian:bookworm-slim.",
    )
    default_start_args: tuple[str, ...] = Field(
        default=(),
        description="Default docker run arguments applied to all containers (e.g., '--cpus=2', '--memory=4g')",
    )
    docker_runtime: str | None = Field(
        default=None,
        description=(
            "Container runtime to pass to `docker run --runtime` (e.g. 'runsc' for gVisor). "
            "When None (the default), no `--runtime` flag is added and Docker uses its configured "
            "default (normally 'runc'). The named runtime must be installed and registered with the "
            "Docker daemon on the host, otherwise container creation fails with Docker's native "
            "'unknown runtime' error. Override per-invocation/environment via "
            "MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME (e.g. set to 'runc' to force the default runtime "
            "where gVisor is unavailable, such as CI)."
        ),
    )
    default_idle_timeout: int = Field(
        default=800,
        description="Default host idle timeout in seconds",
    )
    default_idle_mode: IdleMode = Field(
        default=IdleMode.IO,
        description="Default idle mode for hosts",
    )
    default_activity_sources: tuple[ActivitySource, ...] = Field(
        default_factory=lambda: tuple(ActivitySource),
        description="Default activity sources that count toward keeping host active",
    )
    builder: DockerBuilder = Field(
        default=DockerBuilder.DOCKER,
        description=(
            "Image builder. DOCKER (default) runs native `docker build`. "
            "DEPOT runs `depot build --load` (requires depot CLI + DEPOT_TOKEN in env)."
        ),
    )
    build_timeout_seconds: int = Field(
        default=600,
        description=(
            "Maximum time (in seconds) to wait for `docker build` to finish before aborting. "
            "Increase this when your Dockerfile pulls large bases or downloads heavy assets "
            "(e.g. browser binaries) that would otherwise exceed the default."
        ),
    )
    is_host_volume_created: bool = Field(
        default=True,
        description=(
            "Whether to mount a persistent volume for the host directory. "
            "When True, the host_dir inside each container is backed by a "
            "sub-folder of the shared Docker named volume, making data "
            "accessible even when the container is stopped."
        ),
    )
    isolate_host_volumes: bool | None = Field(
        default=None,
        description=(
            "Whether each host container should see only its own host_dir sub-folder, "
            "rather than the entire shared state volume. When True, mngr mounts the "
            "host's per-host sub-folder directly at host_dir using "
            "`--mount type=volume,...,volume-subpath=...` (requires Docker Engine >= 25.0) "
            "and no longer symlinks host_dir to a path under /mngr-state. "
            "When False, mngr uses today's behavior: the entire shared state volume is "
            "mounted at /mngr-state and host_dir is a symlink into it. "
            "When None (default), today's behavior is used and a one-shot deprecation "
            "warning is emitted noting that the default will change to True in a future "
            "release. Set explicitly to False to keep the legacy behavior silently."
        ),
    )

    @model_validator(mode="after")
    def _validate_isolation_requires_volume(self) -> "DockerProviderConfig":
        if self.isolate_host_volumes is True and not self.is_host_volume_created:
            raise DockerConfigValidationError(
                "isolate_host_volumes=True requires is_host_volume_created=True "
                "(host-volume isolation is meaningless without a host volume)"
            )
        return self

    @model_validator(mode="after")
    def _maybe_warn_about_isolate_default(self) -> "DockerProviderConfig":
        if self.isolate_host_volumes is None:
            _emit_isolate_default_warning_once()
        return self
