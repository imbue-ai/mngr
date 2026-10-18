"""Modal provider plugin entry point.

This module is the ``modal`` setuptools entry point, so it is imported for every
``mngr`` invocation while plugins are loaded. It must therefore stay free of the
heavy ``modal`` SDK: the backend implementation (``imbue.mngr_modal.backend``) and
the provider instance (``imbue.mngr_modal.instance``) pull the ``modal`` SDK in
transitively (via ``imbue.modal_proxy``), so they are imported lazily -- only when
a Modal provider is actually used -- via the ``LazyProviderBackend`` loader below.

Unlike the GCP plugin, Modal has no operator CLI group, but it does contribute two
extra lifecycle hooks -- ``get_files_for_deploy`` and ``on_agent_created``. Only the
entry module's hooks are registered, so all three hooks live here. Deploy-file
collection needs only light helpers (imported at module top); the
``on_agent_created`` snapshot hook needs the SDK-heavy provider instance, so it
imports that lazily inside the hook body (that hook runs at agent-created time,
never at startup).
"""

from pathlib import Path
from typing import Final

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_backend import LazyProviderBackend
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.providers.deploy_utils import collect_provider_profile_files
from imbue.mngr.providers.host_access_manifest import keypair_file_names
from imbue.mngr_modal import hookimpl
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.constants import MODAL_KNOWN_HOSTS_NAME
from imbue.mngr_modal.constants import MODAL_SSH_KEY_NAME

MODAL_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("modal")

MODAL_BUILD_ARGS_HELP: Final[str] = """\
Supported build arguments for the modal provider:
  --file PATH           Path to the Dockerfile to build the sandbox image. Default: Dockerfile in context dir
  --context-dir PATH    Build context directory for Dockerfile COPY/ADD instructions. Default: Dockerfile's directory
  --cpu COUNT           Number of CPU cores (0.25-16). Default: 1.0
  --memory GB           Memory in GB (0.5-32). Default: 1.0
  --gpu TYPE            GPU type to use (e.g., t4, a10g, a100, any). Default: no GPU
  --image NAME          Base Docker image to use. Not required if using --file. Default: debian:bookworm-slim
  --timeout SEC         Maximum sandbox lifetime in seconds. Default: 900 (15 min)
  --region NAME         Region to run the sandbox in (e.g., us-east, us-west, eu-west). Default: auto
  --secret VAR          Pass an environment variable as a secret to the image build. The value of
                        VAR is read from your current environment and made available during Dockerfile
                        RUN commands via --mount=type=secret,id=VAR. Can be specified multiple times.
  --offline             Block all outbound network access from the sandbox [experimental]. Default: off
  --cidr-allowlist CIDR Restrict network access to the specified CIDR range (e.g., 203.0.113.0/24) [experimental].
                        Can be specified multiple times.
  --volume NAME:PATH    Mount a persistent Modal Volume at PATH inside the sandbox [experimental]. NAME is the
                        volume name on Modal (created if it doesn't exist). Can be specified
                        multiple times.
  --docker-build-arg KEY=VALUE
                        Set the value of a Dockerfile ARG. For example,
                        --docker-build-arg=CLAUDE_CODE_VERSION=2.1.50 sets the CLAUDE_CODE_VERSION
                        ARG during the image build. Can be specified multiple times.
"""

MODAL_START_ARGS_HELP: Final[str] = "No start arguments are supported for the modal provider."

# File names under an instance's ``providers/modal/<instance>/keys/`` that must not be baked into
# deployment images: the client key that opens a sandbox (the per-host one under
# ``host_keys/<hex>/`` and the operator-installed shared one) and every known_hosts store or
# per-host link. The sandbox sshd host keys (``host_key``) are shipped: they are already present
# remotely, which is the whole point of them.
_MODAL_EXCLUDED_PROFILE_FILES: Final[frozenset[str]] = frozenset(
    (*keypair_file_names(MODAL_SSH_KEY_NAME), MODAL_KNOWN_HOSTS_NAME)
)


def _load_modal_backend() -> type[ProviderBackendInterface]:
    """Import and return the Modal provider backend class (pulls the ``modal`` SDK)."""
    from imbue.mngr_modal.backend import ModalProviderBackend

    return ModalProviderBackend


@hookimpl
def register_provider_backend() -> LazyProviderBackend:
    """Register the Modal provider backend lazily so startup skips the ``modal`` SDK."""
    return LazyProviderBackend(
        name=MODAL_BACKEND_NAME,
        config_class=ModalProviderConfig,
        load=_load_modal_backend,
        build_args_help=MODAL_BUILD_ARGS_HELP,
        start_args_help=MODAL_START_ARGS_HELP,
    )


@hookimpl
def get_files_for_deploy(
    mngr_ctx: MngrContext,
    include_user_settings: bool,
    include_project_settings: bool,
    repo_root: Path,
) -> dict[Path, Path | str]:
    """Include modal provider profile files, excluding the client SSH keys and known_hosts.

    The client keys (``modal_ssh_key`` and its ``.pub``) and the known_hosts
    stores are environment-specific secrets; the deployed environment mints
    its own per-host client key (``load_or_create_per_host_client_keypair``)
    and pins hosts afresh. The sandbox host keys are included.
    """
    if not include_user_settings:
        return {}
    return collect_provider_profile_files(mngr_ctx, "modal", _MODAL_EXCLUDED_PROFILE_FILES)


@hookimpl
def on_agent_created(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """We need to snapshot the sandbox after the agents are created and initial messages are delivered."""
    # Imported lazily: ``Host`` pulls paramiko/pyinfra and ``ModalProviderInstance``
    # pulls the ``modal`` SDK, neither of which belongs on the startup import path.
    # This hook only runs at agent-created time, so the deferred imports are free.
    from imbue.mngr.hosts.host import Host
    from imbue.mngr_modal.instance import ModalProviderInstance

    if not isinstance(host, Host):
        raise MngrError("Host is not an instance of Host class")

    provider_instance = host.provider_instance
    if isinstance(provider_instance, ModalProviderInstance):
        provider_instance.on_agent_created(agent, host)
