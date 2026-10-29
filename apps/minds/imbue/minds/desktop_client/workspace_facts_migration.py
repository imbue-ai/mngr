"""One-time move of the per-workspace facts into the facts cache (spec section 7.5, phase 3B).

On the first start of a phase-3B build, the per-workspace values each install used to keep for
itself -- the skipped update targets in ``config.toml``, the update-row dismissal files under
``update_dismissals/``, and the backup-verification markers under
``backup_verification_disabled/`` -- become dirty records in the per-workspace facts cache,
which writes each one into its workspace the next time that workspace runs. The old files and
keys are removed once seeded: the cache's dirty records are durable, so nothing is lost before
the write-through lands. A workspace that already has a cache record (a later start) is never
re-seeded, so the whole thing is a no-op from the second start on.
"""

# CLEANUP: remove this module, its MindsConfig helpers, and its call in cli/run.py once every
# supported install has started a phase-3B build.

import json
import shutil
from pathlib import Path

from loguru import logger

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceUpdateDismissals
from imbue.mngr.primitives import AgentId

_LEGACY_DISMISSALS_DIRNAME = "update_dismissals"
_LEGACY_VERIFICATION_DISABLED_DIRNAME = "backup_verification_disabled"


def _read_legacy_dismissals(dismissals_dir: Path) -> dict[str, WorkspaceUpdateDismissals]:
    dismissals_by_agent: dict[str, WorkspaceUpdateDismissals] = {}
    if not dismissals_dir.is_dir():
        return dismissals_by_agent
    for path in sorted(dismissals_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Skipping the unreadable legacy dismissal record {}: {}", path, e)
            continue
        if not isinstance(raw, dict):
            continue
        try:
            dismissals = WorkspaceUpdateDismissals.model_validate(
                {
                    "note_run_started_at": raw.get("note_run_started_at"),
                    "outcome_run_started_at": raw.get("outcome_run_started_at"),
                }
            )
        except ValueError as e:
            logger.warning("Skipping the malformed legacy dismissal record {}: {}", path, e)
            continue
        dismissals_by_agent[path.stem] = dismissals
    return dismissals_by_agent


def _list_legacy_verification_disabled(markers_dir: Path) -> set[str]:
    if not markers_dir.is_dir():
        return set()
    return {path.name for path in markers_dir.iterdir() if path.is_file()}


def _remove_legacy_dir(legacy_dir: Path) -> None:
    if not legacy_dir.is_dir():
        return
    try:
        shutil.rmtree(legacy_dir)
    except OSError as e:
        logger.warning("Could not remove the migrated legacy store at {}: {}", legacy_dir, e)


def migrate_per_workspace_facts(
    minds_config: MindsConfig, facts_cache: WorkspaceFactsCache, paths: InstallationPaths
) -> bool:
    """Seed the facts cache from the pre-3B stores, then remove them; returns whether anything moved.

    Seeds are dirty, so the scheduler and the detection sweep write each workspace's values
    into it the next time it runs. A workspace with an existing cache record keeps it.
    """
    skipped_by_agent = minds_config.read_skipped_update_targets_for_migration()
    dismissals_dir = paths.data_dir / _LEGACY_DISMISSALS_DIRNAME
    markers_dir = paths.data_dir / _LEGACY_VERIFICATION_DISABLED_DIRNAME
    dismissals_by_agent = _read_legacy_dismissals(dismissals_dir)
    disabled_agents = _list_legacy_verification_disabled(markers_dir)
    agent_id_strs = set(skipped_by_agent) | set(dismissals_by_agent) | disabled_agents
    is_anything_moved = False
    for agent_id_str in sorted(agent_id_strs):
        try:
            agent_id = AgentId(agent_id_str)
        except ValueError:
            logger.warning("Skipping a legacy per-workspace fact with a non-agent-id key: {}", agent_id_str)
            continue
        is_seeded = facts_cache.seed_from_legacy_stores_if_absent(
            agent_id,
            skipped_target_ref=skipped_by_agent.get(agent_id_str, ""),
            dismissals=dismissals_by_agent.get(agent_id_str),
            is_backup_verification_disabled=agent_id_str in disabled_agents,
        )
        is_anything_moved = is_anything_moved or is_seeded
    if skipped_by_agent:
        minds_config.delete_skipped_update_targets_for_migration()
    _remove_legacy_dir(dismissals_dir)
    _remove_legacy_dir(markers_dir)
    if is_anything_moved:
        logger.info(
            "Moved the per-workspace facts of {} workspace(s) into the facts cache; they reach each "
            "workspace the next time it runs",
            len(agent_id_strs),
        )
    return is_anything_moved
