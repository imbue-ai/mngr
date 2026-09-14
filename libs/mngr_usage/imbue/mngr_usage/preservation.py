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
from imbue.mngr.api.preservation import ManifestReadStatus
from imbue.mngr.api.preservation import PreservationManifest
from imbue.mngr.api.preservation import PreservationOutcome
from imbue.mngr.api.preservation import PreservedAgentIdentity
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import PreservedItemResult
from imbue.mngr.api.preservation import get_local_preserved_agent_dir_for_host
from imbue.mngr.api.preservation import preserve_agent_data
from imbue.mngr.api.preservation import read_preservation_manifest
from imbue.mngr.api.preservation import read_preservation_manifest_result
from imbue.mngr.api.preservation import write_preservation_manifest
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.provider_instance import build_agent_details_from_offline_ref
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.cel_utils import apply_compiled_cel_filters
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr_usage.data_types import EVENTS_DIR_NAME
from imbue.mngr_usage.data_types import USAGE_DIR_NAME

# data.json lives at the agent state dir root; preserved verbatim so the reader
# can reconstruct enough of an AgentDetails to evaluate filters.
_DATA_JSON_FILENAME = "data.json"

# Legacy archives store host metadata in this sidecar; new archives use the
# shared preservation manifest.
# CLEANUP: drop this name and the manifest-less branch of discover_preserved_agents that
# reads it once no local preserved/ directory still holds an archive written before the
# manifest existed -- this sidecar is no longer written, so it goes as old archives age out.
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
    provider_name: ProviderInstanceName,
    host_id: HostId,
    host_name: HostName,
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

    dest_root = get_local_preserved_agent_dir_for_host(mngr_ctx, agent_name, agent_id, host_id)
    with log_span("Preserving usage data for agent {}", agent_name):
        results = preserve_agent_data(items, source, agent_state_dir, dest_root, mngr_ctx)
        _write_core_manifest(
            dest_root,
            agent_name=agent_name,
            agent_id=agent_id,
            provider_name=provider_name,
            host_id=host_id,
            host_name=host_name,
            results=results,
        )


def _write_core_manifest(
    dest_root: Path,
    *,
    agent_name: AgentName,
    agent_id: AgentId,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    host_name: HostName,
    results: tuple[PreservedItemResult, ...],
) -> None:
    """Add current usage outcomes without deriving identity from stale ``data.json`` bytes."""
    is_data_copied = any(
        result.rel_path == _DATA_JSON_FILENAME and result.outcome == PreservationOutcome.COPIED for result in results
    )
    identity = (
        _identity_from_copied_data(dest_root, provider_name=provider_name, host_id=host_id, host_name=host_name)
        if is_data_copied
        else _existing_same_instance_identity(dest_root, agent_id=agent_id, host_id=host_id)
    )
    if identity is None:
        return
    if identity.agent_id != agent_id or identity.host_id != host_id:
        logger.warning(
            "Refusing to write usage preservation outcomes for {}: archive identity is {}@{}",
            agent_name,
            identity.agent_id,
            identity.host_id,
        )
        return
    write_preservation_manifest(dest_root, PreservationManifest(identity=identity, items=results))


def _identity_from_copied_data(
    dest_root: Path,
    *,
    provider_name: ProviderInstanceName,
    host_id: HostId,
    host_name: HostName,
) -> PreservedAgentIdentity | None:
    """Build identity only when the caller established that this attempt copied ``data.json``."""
    data = _read_json_file(dest_root / _DATA_JSON_FILENAME)
    if data is None:
        return None
    try:
        return PreservedAgentIdentity(
            host_id=host_id,
            host_name=host_name,
            provider_name=provider_name,
            agent_id=AgentId(str(data["id"])),
            agent_name=AgentName(str(data["name"])),
            agent_type=AgentTypeName(str(data["type"])),
            labels=dict(data.get("labels", {})),
        )
    except (KeyError, TypeError, ValueError) as e:
        # Without an identity nothing is written, and the archive stops being discoverable at
        # all, so a data.json we cannot read has to be visible rather than silently dropped.
        logger.warning("Ignoring unusable preserved data.json in {}: {}", dest_root, e)
        return None


def _existing_same_instance_identity(
    dest_root: Path,
    *,
    agent_id: AgentId,
    host_id: HostId,
) -> PreservedAgentIdentity | None:
    """Return identity from a valid existing manifest only when its concrete instance matches."""
    manifest = read_preservation_manifest(dest_root)
    if manifest is None:
        return None
    identity = manifest.identity
    if identity.agent_id != agent_id or identity.host_id != host_id:
        return None
    return identity


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


def _declares_usage(manifest: PreservationManifest) -> bool:
    """Return whether the manifest records an attempted usage-directory preservation."""
    return any(_is_usage_dir_rel_path(Path(result.rel_path)) for result in manifest.items)


def _is_usage_dir_rel_path(rel_path: Path) -> bool:
    """Whether a preserved item is one agent type's usage directory: ``events/<type>/usage``."""
    parts = rel_path.parts
    return len(parts) == 3 and parts[0] == EVENTS_DIR_NAME and parts[2] == USAGE_DIR_NAME


def _manifest_filter_data(identity: PreservedAgentIdentity, data: dict[str, Any] | None) -> dict[str, Any]:
    """Combine optional detailed agent data with the manifest's authoritative identity fields."""
    combined = {} if data is None else dict(data)
    combined.update(
        {
            "id": str(identity.agent_id),
            "name": str(identity.agent_name),
            "type": str(identity.agent_type),
            "labels": identity.labels,
        }
    )
    return combined


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

    New archives are identified by usage-directory outcomes in the shared
    manifest. Archives without a manifest use the legacy usage sidecar.
    When any filter is active, preserved ``data.json`` supplies fields outside
    the manifest identity; an agent that cannot be reconstructed is skipped.
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
        manifest_result = read_preservation_manifest_result(agent_dir, raise_on_io_error=False)
        if manifest_result.status == ManifestReadStatus.UNUSABLE:
            # A manifest that is there but unusable leaves the archive out entirely: falling
            # back to the legacy sidecars would answer with data the manifest supersedes.
            continue
        if manifest_result.manifest is not None:
            if not _declares_usage(manifest_result.manifest):
                continue
            identity = manifest_result.manifest.identity
            data = _manifest_filter_data(identity, _read_json_file(agent_dir / _DATA_JSON_FILENAME))
            meta = {
                "provider_name": str(identity.provider_name),
                "host_id": str(identity.host_id),
                "host_name": str(identity.host_name),
            }
            agent_id = identity.agent_id
            agent_name = identity.agent_name
        else:
            # CLEANUP: remove this branch with the rest of the pre-manifest archive support
            # (see _PRESERVED_META_FILENAME).
            meta = _read_json_file(agent_dir / _PRESERVED_META_FILENAME)
            if meta is None:
                # No usage sidecar -> not preserved by this plugin (or no usage data).
                continue
            data = _read_json_file(agent_dir / _DATA_JSON_FILENAME)
            if data is None or "id" not in data or "name" not in data:
                logger.debug("Skipping preserved dir {} with missing/invalid data.json", agent_dir)
                continue
            try:
                agent_id = AgentId(str(data["id"]))
                agent_name = AgentName(str(data["name"]))
            except ValueError as e:
                logger.debug("Skipping preserved dir {} whose data.json names no usable agent: {}", agent_dir, e)
                continue
        if has_filters:
            details = _agent_details_from_preserved(data, meta)
            if details is None or not _passes_filters(details, compiled_include, compiled_exclude, provider_names):
                continue
        refs.append(PreservedAgentRef(agent_id=str(agent_id), agent_name=str(agent_name), preserved_dir=agent_dir))
    return refs
