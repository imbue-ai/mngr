"""Preserve usage events on destroy, and discover preserved agents for read-back.

When an agent (or its whole host) is destroyed, its state directory -- including
``events/<source>/usage/events.jsonl`` -- is deleted. To keep destroyed agents'
spend visible in ``mngr usage``, this module copies each agent's usage event
directories (plus its ``data.json``, so filters can still apply) to the local
preserved-files location *before* the state directory disappears, reusing the
source-agnostic :mod:`imbue.mngr.api.preservation` core.

It is agent-agnostic: the destroy hooks fire for every agent type, but only
agents that actually wrote usage events (an ``events/<source>/usage`` directory
exists) produce a preserved copy. Non-writers are silently skipped, so no
``mngr_claude`` (or other writer) coupling is needed.

The read side (:func:`discover_preserved_agents`) walks the preserved location,
reconstructs a minimal :class:`AgentDetails` from each preserved ``data.json``
plus the captured host metadata, and applies the same CEL / provider filters
``mngr usage`` would apply to live agents -- so ``--project`` / ``--provider`` /
``--local`` / label filters constrain destroyed agents uniformly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.api.list import build_agent_cel_context
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import get_local_preserved_agent_dir
from imbue.mngr.api.preservation import preserve_agent_data
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.provider_instance import build_agent_details_from_offline_ref
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.cel_utils import apply_compiled_cel_filters
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr_usage.data_types import EVENTS_DIR_NAME
from imbue.mngr_usage.data_types import USAGE_DIR_NAME

# data.json lives at the agent state dir root; preserved verbatim so the reader
# can reconstruct enough of an AgentDetails to evaluate filters.
_DATA_JSON_FILENAME = "data.json"

# Sidecar written into the preserved agent dir alongside the mirrored state-dir
# layout. data.json does not record which provider/host the agent ran on
# (provider is implied by where the live host dir lived), so we capture it at
# preserve time -- it's what host-scoped filters (--provider / --local) need.
_PRESERVED_META_FILENAME = "mngr_usage_meta.json"


class PreservedAgentRef(FrozenModel):
    """A destroyed agent whose usage events were preserved locally.

    ``preserved_dir`` is the agent's directory under
    ``<local_host_dir>/preserved/``; its ``events/<source>/usage/`` subtree
    mirrors the live state-dir layout. ``agent_id`` keys per-agent aggregation
    (and dedup against still-live agents).
    """

    agent_id: str = Field(description="The destroyed agent's id, from its preserved data.json")
    agent_name: str = Field(description="The destroyed agent's name, from its preserved data.json")
    preserved_dir: Path = Field(description="Directory under <local_host_dir>/preserved/ holding this agent's files")


# =============================================================================
# Write side: preserve on destroy
# =============================================================================


def _discover_usage_items(source: HostFileReadInterface, agent_state_dir: Path) -> list[PreservedItem]:
    """Return one DIRECTORY :class:`PreservedItem` per ``events/<source>/usage`` dir that exists.

    Lists the agent's ``events/`` dir on ``source`` and, for each source
    subdirectory, includes its ``usage`` directory when present. Returns an
    empty list when the agent wrote no usage events (e.g. a non-Claude agent),
    which the caller treats as "nothing to preserve".
    """
    events_dir = agent_state_dir / EVENTS_DIR_NAME
    items: list[PreservedItem] = []
    for entry in source.list_directory(events_dir):
        if entry.file_type != FileType.DIRECTORY:
            continue
        source_name = Path(entry.path).name
        usage_rel = f"{EVENTS_DIR_NAME}/{source_name}/{USAGE_DIR_NAME}"
        if source.path_exists(agent_state_dir / usage_rel):
            items.append(PreservedItem(rel_path=usage_rel, kind=FileType.DIRECTORY))
    return items


def preserve_agent_usage(
    source: HostFileReadInterface,
    agent_state_dir: Path,
    agent_name: AgentName,
    agent_id: AgentId,
    *,
    provider_name: str,
    host_id: str,
    host_name: str,
    mngr_ctx: MngrContext,
) -> None:
    """Preserve one agent's usage events (and data.json) before its state dir is deleted.

    No-op when the agent has no usage events, so this can be called for every
    agent type without creating spurious preserved directories. Failures for
    any single item are logged and swallowed by :func:`preserve_agent_data` so
    they never abort the destruction that triggered this.
    """
    items = _discover_usage_items(source, agent_state_dir)
    if not items:
        return
    items.append(PreservedItem(rel_path=_DATA_JSON_FILENAME, kind=FileType.FILE))

    dest_root = get_local_preserved_agent_dir(mngr_ctx, agent_name, agent_id)
    with log_span("Preserving usage data for agent {}", agent_name):
        preserve_agent_data(items, source, agent_state_dir, dest_root, mngr_ctx)
        _write_preserved_meta(dest_root, provider_name=provider_name, host_id=host_id, host_name=host_name)


def _write_preserved_meta(dest_root: Path, *, provider_name: str, host_id: str, host_name: str) -> None:
    """Write the host-metadata sidecar into the preserved agent dir."""
    dest_root.mkdir(parents=True, exist_ok=True)
    meta = {"provider_name": provider_name, "host_id": host_id, "host_name": host_name}
    (dest_root / _PRESERVED_META_FILENAME).write_text(json.dumps(meta, indent=2) + "\n")


# =============================================================================
# Read side: discover preserved agents and apply filters
# =============================================================================


def _read_json_file(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from ``path``; return None if missing or not a JSON object.

    A corrupt preserved file is a genuine anomaly (we wrote it ourselves), so a
    malformed JSON body is logged at warning level rather than swallowed.
    """
    try:
        content = path.read_text()
    except OSError:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning("Ignoring corrupt preserved JSON file {}: {}", path, e)
        return None
    return parsed if isinstance(parsed, dict) else None


def _agent_details_from_preserved(data: dict[str, Any], meta: dict[str, Any]) -> AgentDetails | None:
    """Rebuild an :class:`AgentDetails` from a preserved data.json + host meta.

    Wraps the preserved ``data.json`` in a :class:`DiscoveredAgent` (whose typed
    properties already parse ``type`` / ``work_dir`` / ``command`` / ``labels`` /
    etc. from that dict) and hands it to ``build_agent_details_from_offline_ref``
    -- the same construction the ``list_agents`` offline path uses for
    destroyed/unreachable agents. So the reconstructed CEL context matches the
    offline list path field-for-field, and the host metadata captured at
    preserve time (provider/host) populates ``host.*`` for ``--provider`` /
    ``--local`` filters. Returns None if the record is too malformed to
    reconstruct.
    """
    try:
        host_id = HostId(str(meta["host_id"]))
        provider_name = ProviderInstanceName(str(meta["provider_name"]))
        ref = DiscoveredAgent(
            host_id=host_id,
            agent_id=AgentId(str(data["id"])),
            agent_name=AgentName(str(data["name"])),
            provider_name=provider_name,
            certified_data=data,
        )
        host_details = HostDetails(id=host_id, name=str(meta["host_name"]), provider_name=provider_name)
    except (KeyError, ValueError, ValidationError) as e:
        logger.debug("Could not reconstruct AgentDetails from preserved data.json: {}", e)
        return None
    return build_agent_details_from_offline_ref(ref, host_details)


def _passes_filters(
    details: AgentDetails,
    compiled_include: Sequence[Any],
    compiled_exclude: Sequence[Any],
    provider_names: tuple[str, ...] | None,
) -> bool:
    """Apply the same provider + CEL filters ``mngr usage`` applies to live agents."""
    if provider_names is not None and str(details.host.provider_name) not in provider_names:
        return False
    return apply_compiled_cel_filters(
        cel_context=build_agent_cel_context(details),
        include_filters=compiled_include,
        exclude_filters=compiled_exclude,
        error_context_description=f"preserved agent {details.name}",
    )


def discover_preserved_agents(
    mngr_ctx: MngrContext,
    *,
    include_filters: Sequence[str] = (),
    exclude_filters: Sequence[str] = (),
    provider_names: tuple[str, ...] | None = None,
) -> list[PreservedAgentRef]:
    """Return preserved agents (under ``<local_host_dir>/preserved/``) matching the filters.

    ``include_filters`` / ``exclude_filters`` are raw CEL strings (the same form
    ``gather_usage_snapshots`` / ``list_agents`` take); they're compiled here
    once and evaluated against each preserved agent's reconstructed context.

    Each candidate dir must carry the usage sidecar this module writes; dirs
    preserved by other plugins (e.g. ``mngr_claude`` session preservation) but
    with no usage data are skipped. When any filter is active, an agent whose
    ``data.json`` cannot be reconstructed into an :class:`AgentDetails` is
    skipped (we cannot verify it matches); with no filters, all preserved
    usage-bearing agents are returned.
    """
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    preserved_root = local_host_dir / "preserved"
    if not preserved_root.is_dir():
        return []

    has_filters = bool(include_filters or exclude_filters or provider_names)
    compiled_include, compiled_exclude = compile_cel_filters(tuple(include_filters), tuple(exclude_filters))
    refs: list[PreservedAgentRef] = []
    for agent_dir in sorted(preserved_root.iterdir()):
        if not agent_dir.is_dir():
            continue
        meta = _read_json_file(agent_dir / _PRESERVED_META_FILENAME)
        if meta is None:
            # No usage sidecar -> not preserved by this plugin (or no usage data).
            continue
        data = _read_json_file(agent_dir / _DATA_JSON_FILENAME)
        if data is None or "id" not in data or "name" not in data:
            logger.debug("Skipping preserved dir {} with missing/invalid data.json", agent_dir)
            continue
        if has_filters:
            details = _agent_details_from_preserved(data, meta)
            if details is None or not _passes_filters(details, compiled_include, compiled_exclude, provider_names):
                continue
        refs.append(PreservedAgentRef(agent_id=str(data["id"]), agent_name=str(data["name"]), preserved_dir=agent_dir))
    return refs
