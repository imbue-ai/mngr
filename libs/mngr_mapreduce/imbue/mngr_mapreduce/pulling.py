"""Outputs-archive polling and extraction.

Every map-reduce agent (mapper or reducer) publishes a tarball at a
framework-defined path under ``$MNGR_AGENT_STATE_DIR``. The orchestrator
polls for that file via the provider's agent volume, then extracts it
under ``<output_dir>/<agent_name>/``. Contents of the archive are opaque
to the framework; the recipe's ``on_*_finalized`` hooks interpret them.
"""

import io
import tarfile
from pathlib import Path

from loguru import logger

from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.volume import Volume
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_mapreduce.archive import ARCHIVE_SUBPATH


def _get_agent_volume(
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    agent_id: AgentId,
) -> Volume | None:
    """Return the volume scoped to a single agent's state directory.

    Returns None when the provider does not expose a host volume (e.g. SSH)
    or when the volume cannot otherwise be resolved.
    """
    try:
        provider = get_provider_instance(provider_name, mngr_ctx)
        host_volume = provider.get_volume_for_host(host_id)
    except (MngrError, OSError) as exc:
        logger.warning("Failed to resolve volume for host {}: {}", host_id, exc)
        return None
    if host_volume is None:
        return None
    return host_volume.get_agent_volume(agent_id)


def is_agent_outputs_ready(
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    agent_id: AgentId,
) -> bool:
    """Check whether the agent has finished writing its outputs archive.

    Matches the final archive name exactly so the partially-written ``.tmp``
    file (which the agent renames on completion) is not mistaken for the
    finished output.
    """
    agent_volume = _get_agent_volume(mngr_ctx, provider_name, host_id, agent_id)
    if agent_volume is None:
        return False
    try:
        return agent_volume.path_exists(ARCHIVE_SUBPATH)
    except (MngrError, OSError):
        return False


def pull_agent_outputs(
    mngr_ctx: MngrContext,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    agent_id: AgentId,
    agent_name: AgentName,
    destination_dir: Path,
) -> Path | None:
    """Download and extract the outputs archive for a single agent.

    Reads the archive from the agent's state volume and extracts it under
    ``destination_dir/<agent_name>/``. Returns the extraction directory on
    success, None on any failure. The extracted contents are treated as
    opaque -- the recipe's hook is responsible for interpreting them.
    """
    agent_volume = _get_agent_volume(mngr_ctx, provider_name, host_id, agent_id)
    if agent_volume is None:
        logger.warning(
            "Cannot pull outputs for agent '{}': provider '{}' does not expose a volume",
            agent_name,
            provider_name,
        )
        return None

    try:
        archive_bytes = agent_volume.read_file(ARCHIVE_SUBPATH)
    except (MngrError, OSError) as exc:
        logger.warning("Failed to read outputs archive for agent '{}': {}", agent_name, exc)
        return None

    local_dest = destination_dir / str(agent_name)
    local_dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            tar.extractall(local_dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Failed to extract outputs archive for agent '{}': {}", agent_name, exc)
        return None
    logger.info("Extracted outputs archive for agent '{}' to {}", agent_name, local_dest)
    return local_dest
