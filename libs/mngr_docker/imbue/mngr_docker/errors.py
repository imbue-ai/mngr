from imbue.mngr.errors import ConfigError
from imbue.mngr.errors import HostCreationError
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderInstanceName


class DockerBuildTimeoutError(HostCreationError):
    """Raised when `docker build` exceeds the configured build timeout."""

    def __init__(self, provider_name: ProviderInstanceName, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            provider_name,
            f"docker build timed out after {timeout_seconds} seconds for provider '{provider_name}'.",
        )
        self.user_help_text = (
            f"Increase build_timeout_seconds for this provider, e.g.:\n"
            f"  mngr config set --scope user providers.{provider_name}.build_timeout_seconds 1800"
        )


class DockerRuntimeNotRegisteredError(HostCreationError):
    """Raised when the configured `docker_runtime` is not registered with the Docker daemon.

    Surfaces Docker's native "unknown or invalid runtime name" failure as a
    clean, actionable message instead of a raw `ProcessError` traceback that
    buries the cause inside the full `docker run` command line.
    """

    def __init__(self, provider_name: ProviderInstanceName, runtime_name: str) -> None:
        self.runtime_name = runtime_name
        super().__init__(
            provider_name,
            f"Docker runtime '{runtime_name}' is not registered with the Docker daemon "
            f"for provider '{provider_name}'.",
        )
        self.user_help_text = (
            f"Install and register the '{runtime_name}' runtime with Docker (e.g. gVisor's "
            f"runsc), or select the default runtime by setting docker_runtime to 'runc':\n"
            f"  mngr config set --scope user providers.{provider_name}.docker_runtime runc\n"
            f"or per-invocation:\n"
            f"  MNGR__PROVIDERS__{provider_name.upper()}__DOCKER_RUNTIME=runc"
        )


class DockerGvisorEphemeralRootfsError(HostCreationError):
    """Raised when a configured gVisor (`runsc`) Docker runtime lacks `--overlay2=none`.

    gVisor's default root overlay (`--overlay2=root:self`) keeps each container's root
    filesystem in a per-sandbox overlay that is discarded whenever the container stops,
    so mngr's SSH provisioning (the injected host key, `authorized_keys`, and the
    self-healing-entrypoint marker) is lost the first time the container restarts --
    leaving the host running but unreachable. Registering the runtime with
    `--overlay2=none` writes the root layer through to the persistent Docker layer.
    """

    def __init__(self, provider_name: ProviderInstanceName, runtime_name: str) -> None:
        self.runtime_name = runtime_name
        super().__init__(
            provider_name,
            f"Docker runtime '{runtime_name}' (gVisor) for provider '{provider_name}' is registered "
            f"without '--overlay2=none', so each container's root filesystem is ephemeral: gVisor's "
            f"default overlay ('--overlay2=root:self') discards all root-filesystem writes when the "
            f"container stops, so mngr's SSH provisioning is lost on the first restart and the host "
            f"becomes unreachable.",
        )
        self.user_help_text = (
            f"Fix this in one of two ways:\n"
            f"  1. Run containers under the standard runtime instead of gVisor -- set docker_runtime to 'runc':\n"
            f"       mngr config set --scope user providers.{provider_name}.docker_runtime runc\n"
            f"     (or per-invocation: MNGR__PROVIDERS__{provider_name.upper()}__DOCKER_RUNTIME=runc)\n"
            f"  2. Re-register the gVisor runtime so writes persist, then restart Docker:\n"
            f"       sudo runsc install -- --overlay2=none\n"
            f"       sudo systemctl restart docker\n"
            f"     With option 2, if the container runs supervisord (or anything that installs a unix\n"
            f"     socket via a hard link under /run), ALSO mount /run as a tmpfs -- e.g. set\n"
            f'     default_start_args=["--tmpfs", "/run"] on this provider. --overlay2=none puts /run\n'
            f"     on gVisor's gofer filesystem, where os.link() of a socket fails (EOPNOTSUPP), so\n"
            f"     supervisord wedges and never starts its services without a tmpfs /run."
        )


class DockerConfigValidationError(ConfigError, ValueError):
    """Raised when Docker provider config fields are mutually inconsistent."""


class InvalidContainerSizeError(MngrError, ValueError):
    """Raised when a ContainerSize records a swap cap together with unlimited swap, which docker cannot express."""
