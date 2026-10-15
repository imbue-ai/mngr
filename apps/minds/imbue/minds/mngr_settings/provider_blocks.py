from typing import Final

IMBUE_CLOUD_BACKEND_NAME: Final[str] = "imbue_cloud"

# Runtime knobs written into each account instance block (``[providers.imbue_cloud_<user id>]``) so the imbue_cloud slow (rebuild) path runs the agent container under gVisor with the runsc hardening args.
# These mirror the default-workspace-template ``[providers.ovh]`` bake settings; ``ImbueCloudProviderConfig`` (which extends ``VpsProviderConfig``) forwards them onto the delegated vps_docker provider, and ``install_gvisor_runtime`` also drives the slow path's SSH host-setup so a leased host that lacks runsc has it installed before the container is rebuilt under it.
IMBUE_CLOUD_DOCKER_RUNTIME: Final[str] = "runsc"
IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME: Final[bool] = True
IMBUE_CLOUD_DEFAULT_START_ARGS: Final[tuple[str, ...]] = ("--workdir=/", "--security-opt=no-new-privileges")

# The user-data layout knobs written into every minds-authored vps-based provider block (imbue_cloud and the aws/gcp/azure connection instances), mirroring the default-workspace-template ``[providers.ovh]`` / ``[providers.vultr]`` settings.
# The container's /home/user symlinks onto the unified volume's home/ subdir (the ONE persistent, backed-up tree), mngr's data dir hides inside it, and mngr's plain-text service logs stay off the volume.
WORKSPACE_HOST_DIR: Final[str] = "/home/user/.mngr"
WORKSPACE_VOLUME_HOME_PATH: Final[str] = "/home/user"
WORKSPACE_HOST_LOG_DIR: Final[str] = "/var/log/mngr"

AWS_BACKEND_NAME: Final[str] = "aws"
GCP_BACKEND_NAME: Final[str] = "gcp"
AZURE_BACKEND_NAME: Final[str] = "azure"
MODAL_BACKEND_NAME: Final[str] = "modal"

# Container-hardening knobs written into every cloud connection instance block (aws, gcp, and azure alike).
# The gVisor/runsc settings mirror the default-workspace-template ``[providers.ovh]`` / ``[providers.vultr]`` bake settings so the cloud VM runs the agent in a runsc-hardened container; the matching ``docker run`` start args live in the template's per-cloud ``[create_templates.*]`` blocks, and mngr_vps itself mounts /run and /tmp on tmpfs whenever the runtime is runsc.
CLOUD_CONNECTION_DOCKER_RUNTIME: Final[str] = "runsc"
CLOUD_CONNECTION_INSTALL_GVISOR_RUNTIME: Final[bool] = True
AWS_PROVIDER_NAME_PREFIX: Final[str] = "aws-"

# The value a Modal connection instance's ``mode`` carries (the user's own Modal token); also how a legacy ambient ``[providers.modal]`` block is recognized for its rewrite into the suppression form.
MODAL_MODE_DIRECT: Final[str] = "DIRECT"

# CLEANUP: drop this prefix (and the stale-block warning that names it) once no install can still carry a ``byok-*`` block, i.e. after the first release with connections has been out for a support window.
LEGACY_BYOK_PROVIDER_NAME_PREFIX: Final[str] = "byok-"
