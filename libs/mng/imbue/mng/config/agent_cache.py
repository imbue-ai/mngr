import json
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mng.primitives import AgentReference
from imbue.mng.primitives import HostReference
from imbue.mng.utils.file_utils import atomic_write

AGENT_COMPLETIONS_CACHE_FILENAME: Final[str] = ".agent_completions.json"


class AgentSummaryUntyped(FrozenModel):
    """Cached metadata about an agent for provider-aware lookups."""

    name: str = Field(description="Human-readable agent name")
    id: str = Field(description="Unique agent identifier")
    provider: str = Field(description="Provider instance name that owns the agent's host")
    host_name: str = Field(description="Human-readable host name")
    host_id: str = Field(description="Unique host identifier")


def write_agent_names_cache(
    cache_dir: Path,
    agents_by_host: Mapping[HostReference, Sequence[AgentReference]],
) -> None:
    """Write agent data to the completion cache file (best-effort).

    Writes a JSON file with per-agent metadata (including provider) so that
    shell completion and provider-aware lookups can read it without importing
    the mng config system. A backward-compatible "names" key is also written
    so that the lightweight shell completer (complete.py) continues to work.

    Catches OSError from cache writes so filesystem failures do not break
    the caller. Other exceptions are allowed to propagate.
    """
    try:
        entries: list[AgentSummaryUntyped] = []
        for host_ref, agent_refs in agents_by_host.items():
            for agent_ref in agent_refs:
                entries.append(
                    AgentSummaryUntyped(
                        name=str(agent_ref.agent_name),
                        id=str(agent_ref.agent_id),
                        provider=str(host_ref.provider_name),
                        host_name=str(host_ref.host_name),
                        host_id=str(host_ref.host_id),
                    )
                )

        # Backward-compatible names list for the lightweight shell completer
        names = sorted({entry.name for entry in entries})

        cache_data = {
            "agents": [entry.model_dump() for entry in entries],
            "names": names,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        cache_path = cache_dir / AGENT_COMPLETIONS_CACHE_FILENAME
        atomic_write(cache_path, json.dumps(cache_data))
    except OSError:
        logger.debug("Failed to write agent name completion cache")


def resolve_identifiers_from_cache(
    cache_dir: Path,
    identifiers: Sequence[str],
) -> list[AgentSummaryUntyped] | None:
    """Look up cached agent entries matching the given identifiers (by name or ID).

    Returns a list of AgentSummaryUntyped objects if every identifier is found in
    the cache, or None if the cache is missing/corrupt or any identifier cannot
    be resolved.
    """
    try:
        cache_path = cache_dir / AGENT_COMPLETIONS_CACHE_FILENAME
        if not cache_path.is_file():
            return None
        raw = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    agents_list = raw.get("agents")
    if not isinstance(agents_list, list):
        return None

    # Build lookup dicts: name -> list of entries, id -> list of entries
    entries_by_name: dict[str, list[AgentSummaryUntyped]] = {}
    entries_by_id: dict[str, list[AgentSummaryUntyped]] = {}
    for raw_entry in agents_list:
        if not isinstance(raw_entry, dict):
            continue
        try:
            entry = AgentSummaryUntyped.model_validate(raw_entry)
        except ValueError:
            continue
        entries_by_name.setdefault(entry.name, []).append(entry)
        entries_by_id.setdefault(entry.id, []).append(entry)

    # Resolve each identifier against both name and id lookups
    matched_entries: list[AgentSummaryUntyped] = []
    for identifier in identifiers:
        name_matches = entries_by_name.get(identifier)
        id_matches = entries_by_id.get(identifier)
        if name_matches is None and id_matches is None:
            return None
        if name_matches is not None:
            matched_entries.extend(name_matches)
        if id_matches is not None:
            matched_entries.extend(id_matches)

    return matched_entries
