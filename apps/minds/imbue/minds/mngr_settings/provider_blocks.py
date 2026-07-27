import re
from typing import Final

import tomlkit

from imbue.minds.mngr_settings.errors import MindsSettingsError

IMBUE_CLOUD_BACKEND_NAME: Final[str] = "imbue_cloud"

# Runtime knobs written into each per-account ``[providers.imbue_cloud_<slug>]`` block so the imbue_cloud slow (rebuild) path runs the agent container under gVisor with the runsc hardening args.
# These mirror the default-workspace-template ``[providers.ovh]`` bake settings; ``ImbueCloudProviderConfig`` (which extends ``VpsProviderConfig``) forwards them onto the delegated vps_docker provider, and ``install_gvisor_runtime`` also drives the slow path's SSH host-setup so a leased host that lacks runsc has it installed before the container is rebuilt under it.
IMBUE_CLOUD_DOCKER_RUNTIME: Final[str] = "runsc"
IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME: Final[bool] = True
IMBUE_CLOUD_DEFAULT_START_ARGS: Final[tuple[str, ...]] = ("--workdir=/", "--security-opt=no-new-privileges")

# The user-data layout knobs written into every minds-authored vps-based provider block (imbue_cloud, byok aws/gcp/azure), mirroring the default-workspace-template ``[providers.ovh]`` / ``[providers.vultr]`` settings.
# The container's /home/user symlinks onto the unified volume's home/ subdir (the ONE persistent, backed-up tree), mngr's data dir hides inside it, and mngr's plain-text service logs stay off the volume.
WORKSPACE_HOST_DIR: Final[str] = "/home/user/.mngr"
WORKSPACE_VOLUME_HOME_PATH: Final[str] = "/home/user"
WORKSPACE_HOST_LOG_DIR: Final[str] = "/var/log/mngr"

AWS_BACKEND_NAME: Final[str] = "aws"
GCP_BACKEND_NAME: Final[str] = "gcp"
AZURE_BACKEND_NAME: Final[str] = "azure"

# Container-hardening knobs written into each bring-your-own-key ``[providers.byok-aws-<slug>]`` account block (the only AWS path in minds).
# The gVisor/runsc settings mirror the default-workspace-template ``[providers.ovh]`` / ``[providers.vultr]`` bake settings so the EC2 outer host runs the agent in a runsc-hardened container; the matching ``docker run`` start args live in the template ``[create_templates.aws]``.
AWS_DOCKER_RUNTIME: Final[str] = "runsc"
AWS_INSTALL_GVISOR_RUNTIME: Final[bool] = True
AWS_PROVIDER_NAME_PREFIX: Final[str] = "aws-"

# EC2 instance size for minds AWS workspaces.
# The mngr_aws default (t3.small, 2 GB) is too small for the full default-workspace-template build (uv sync + npm ci/build OOMs/thrashes on 2 GB); minds workspaces default to t3.large (8 GB).
AWS_DEFAULT_INSTANCE_TYPE: Final[str] = "t3.large"
# Mount /run as a tmpfs in the AWS workspace container.
# mngr_aws leaves the container rootfs (and thus /run) on gVisor's gofer-backed 9p filesystem, which returns EOPNOTSUPP for os.link() of a socket inode.
# supervisord installs its control socket via a hard link (bind a temp socket, then os.link it into place at /var/run/supervisor.sock); on AWS that link fails, supervisord misreads it as a stale socket and loops forever ("Unlinking stale socket") without ever starting system_interface / the browser service -- so the workspace reports "unresponsive".
# A tmpfs /run supports the hard link (verified: os.link of a socket succeeds on a tmpfs but fails on the gofer rootfs), so the control socket comes up.
# The ovh/vultr/imbue_cloud paths already get a tmpfs /run via their host setup, which is why this only bites AWS.
AWS_DEFAULT_START_ARGS: Final[tuple[str, ...]] = ("--tmpfs", "/run")

# The single ``[providers.modal]`` instance for "Modal (1-day ephemeral)" (DIRECT mode): the local machine authenticates to Modal with its own token (``modal token new``).
# Written unconditionally at startup so the option works as soon as a token exists; if none is present the provider just reports unavailable during discovery.
# Sizing and sandbox timeout come from the ModalProviderConfig defaults; only ``is_persistent`` is forced.
MODAL_BACKEND_NAME: Final[str] = "modal"
MODAL_PROVIDER_NAME: Final[str] = "modal"
MODAL_MODE_DIRECT: Final[str] = "DIRECT"

# A "cloud account" is one ``[providers.byok-<backend>-<slug>]`` block in the active settings.toml, holding the user's pasted credentials plus the same hardening knobs the ambient blocks get.
# The ``byok-`` prefix keeps these outside the boot reconciler's ``aws-*`` legacy cleanup, so accounts survive restarts.
BYOK_PROVIDER_NAME_PREFIX: Final[str] = "byok-"
BYOK_SUPPORTED_BACKENDS: Final[tuple[str, ...]] = ("aws", "gcp", "azure")


def remove_provider_block(doc: tomlkit.TOMLDocument, provider_name: str) -> bool:
    """Delete ``[providers.<provider_name>]`` from the document; returns whether it was present."""
    providers = doc.get("providers")
    if not isinstance(providers, dict) or provider_name not in providers:
        return False
    del providers[provider_name]
    return True


def _slugify_imbue_cloud_account(email: str) -> str:
    """Slugify an account email for use in a provider instance name.

    A deliberate mirror of ``mngr_imbue_cloud``'s ``slugify_account``, inlined because this package must be importable before mngr (which the plugin transitively pulls in).
    """
    lowered = email.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise MindsSettingsError(f"Cannot slugify imbue_cloud account email: {email!r}")
    return slug


def imbue_cloud_provider_name_for_account(email: str) -> str:
    """Return the provider instance name minds writes for ``email``."""
    return f"imbue_cloud_{_slugify_imbue_cloud_account(email)}"


def _slugify_cloud_account_alias(alias: str) -> str:
    """Slugify an alias for use in the provider block name; raises on an empty result."""
    slug = re.sub(r"[^a-z0-9]+", "-", alias.strip().lower()).strip("-")
    if not slug:
        raise MindsSettingsError(f"Alias {alias!r} contains no usable characters")
    return slug


def cloud_account_provider_name(backend: str, alias: str) -> str:
    """Return the provider block name for a new cloud account (``byok-<backend>-<slug>``)."""
    return f"{BYOK_PROVIDER_NAME_PREFIX}{backend}-{_slugify_cloud_account_alias(alias)}"
