"""Preserve files from an agent's state directory to local storage on destroy.

When an agent (or its whole host) is destroyed, the agent's state directory is
deleted. Some files in it are worth keeping -- session transcripts, logs, etc.
This module provides a single, source-agnostic way to copy a declared set of
those files to a stable local location *before* the state directory disappears.

The set of files to keep is declared once by the caller as a list of
:class:`PreservedItem` (paths relative to the agent state directory). The same
declaration is executed against either:

- an online host (:class:`~imbue.mngr.interfaces.host.OnlineHostInterface`),
  reading over SSH / locally and using rsync for directories, or
- a stopped-but-volume-backed host
  (:class:`~imbue.mngr.hosts.offline_host.OfflineHostWithVolume`), reading from
  the host's persisted volume.

Both are :class:`~imbue.mngr.interfaces.host.HostFileReadInterface`, so callers
do not branch on online-vs-offline: they pass whichever host they hold and the
single :func:`preserve_agent_data` call does the right thing. Preserved files
mirror the agent-state-dir layout verbatim under the destination root.

Destruction is also the last chance to *produce* data worth keeping, so the
online path (:func:`preserve_agent_state`) first runs each transcript
producer's final bounded pass (:func:`flush_agent_transcript`) and then copies.

What was preserved is recorded next to the files as a
:class:`PreservationManifest`: who the agent was and where it ran
(:class:`PreservedAgentIdentity`), what each declared item's copy attempt
observed, and what those final producer passes reported. An archive outlives
every process that could answer those questions, so nothing about it can be
looked up again afterwards.

The read side walks that storage: :func:`discover_preserved_agent_archives`
returns every archive under a local host dir (manifest-backed, and the
pre-manifest ones identified by directory name),
:func:`read_preservation_manifest` reads one archive's manifest, and
:func:`preserved_agent_matches` addresses an archive by agent name or id.
"""

import json
import shlex
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import TypeVar

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import get_agents_root_dir
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasTranscriptMixin
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.file_utils import atomic_write

PRESERVATION_MANIFEST_FILENAME: Final[str] = "manifest.json"
# The manifest layout this version writes and knows how to interpret. An archive whose manifest
# declares a higher version was written by a newer mngr and is never written over from here.
PRESERVATION_MANIFEST_SCHEMA_VERSION: Final[int] = 1

# The transcript producers a plugin provisions into the agent state dir's commands/ directory:
# every transcript plugin provisions this same pair (each declares the names again in its own
# module). The shell helper mngr_common_transcript_flush in resources/mngr_common_transcript_lib.sh
# runs the same two scripts, in the same order, under the same lock timeout -- but it sends both
# streams to /dev/null and ends each invocation with `|| true`, because it runs inside a stop hook
# that must not fail. Destruction needs the opposite: which producer was there, which one failed,
# and what it said, one TranscriptFlushResult per script, for the manifest. Keep the two in step.
_RAW_TRANSCRIPT_SCRIPT_NAME: Final[str] = "stream_transcript.sh"
_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"
# Destruction cannot wait on a producer, so the final pass gets a short wait for the converter
# lock and a hard bound on the command; a pass that does not finish inside them is reported, not
# retried.
_FLUSH_CONVERT_LOCK_TIMEOUT_SECONDS: Final[int] = 2
_FLUSH_COMMAND_TIMEOUT_SECONDS: Final[float] = 15.0
# Errors are copied into the manifest, which is read back and rewritten on every later
# attempt, so only the tail of a large stderr is kept.
_PRESERVED_ERROR_CHAR_LIMIT: Final[int] = 1000
# What an archive from before the manifest carries instead: the agent record every preserved
# agent has, and the host-metadata sidecar the usage plugin used to write beside it.
# CLEANUP: drop these two names, _parse_legacy_archive_name and _read_legacy_usage_identity
# (and the legacy branch in discover_preserved_agent_archives that calls them) once no local
# preserved/ directory still holds an archive written before the manifest existed -- these
# archives are never created any more, so this becomes safe as old ones are cleaned up.
_AGENT_DATA_FILENAME: Final[str] = "data.json"
_USAGE_META_SIDECAR_FILENAME: Final[str] = "mngr_usage_meta.json"


class PreservedItem(FrozenModel):
    """One file or directory to preserve, addressed relative to the agent state dir."""

    rel_path: str = Field(description="Path relative to the agent state directory")
    kind: FileType = Field(description="Whether rel_path is a FILE or a DIRECTORY")


class PreservationOutcome(LowerCaseStrEnum):
    """What one copy attempt observed for one declared preservation item."""

    MISSING = auto()
    COPIED = auto()
    ERROR = auto()


class TranscriptFlushOutcome(LowerCaseStrEnum):
    """What one destruction-time transcript producer pass did."""

    SUCCEEDED = auto()
    SKIPPED = auto()
    ERROR = auto()


class TranscriptFlushResult(FrozenModel):
    """Result of one bounded destruction-time transcript producer pass."""

    # An archive outlives the mngr version that wrote it, so a newer version's additive
    # fields must not make this reader reject the whole record -- see the style guide's
    # "Schema evolution" section.
    model_config = ConfigDict(extra="ignore")

    script_name: str = Field(description="The producer script that was asked for a final pass")
    outcome: TranscriptFlushOutcome = Field(description="Whether the pass ran, was not there to run, or failed")
    flushed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this pass ran, which a later preservation attempt carries forward unchanged",
    )
    error: str | None = Field(default=None, description="The tail of what a failed pass reported")


class PreservedItemResult(FrozenModel):
    """The destruction-time result for one requested preservation item."""

    # An archive outlives the mngr version that wrote it, so a newer version's additive
    # fields must not make this reader reject the whole record -- see the style guide's
    # "Schema evolution" section.
    model_config = ConfigDict(extra="ignore")

    rel_path: str = Field(description="Path relative to the agent state directory")
    kind: FileType = Field(description="Whether rel_path is a FILE or a DIRECTORY")
    outcome: PreservationOutcome = Field(description="Whether this copy attempt found, copied, or failed on the item")
    error: str | None = Field(default=None, description="What a failed copy attempt reported")


class PreservedAgentIdentity(FrozenModel):
    """Who a preserved archive belonged to, and where it ran.

    A fixed set of non-secret fields, copied out of discovery at destruction time: an archive
    outlives the agent and the host, so nothing can be looked up again afterwards.
    """

    # An archive outlives the mngr version that wrote it, so a newer version's additive
    # fields must not make this reader reject the whole record -- see the style guide's
    # "Schema evolution" section.
    model_config = ConfigDict(extra="ignore")

    host_id: HostId = Field(description="The host the agent ran on")
    host_name: HostName = Field(description="That host's name at destruction time")
    provider_name: ProviderInstanceName = Field(description="The provider instance the host belonged to")
    agent_id: AgentId = Field(description="The destroyed agent's id")
    agent_name: AgentName = Field(description="The destroyed agent's name, which a later agent may reuse")
    agent_type: AgentTypeName = Field(description="The destroyed agent's type (e.g. claude, codex)")
    labels: dict[str, str] = Field(default_factory=dict, description="The agent's labels at destruction time")


class PreservationManifest(FrozenModel):
    """Machine-readable inventory written alongside an agent's preserved data."""

    # An archive outlives the mngr version that wrote it, so a newer version's additive
    # fields must not make this reader reject the whole record -- see the style guide's
    # "Schema evolution" section.
    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(
        default=PRESERVATION_MANIFEST_SCHEMA_VERSION,
        ge=1,
        description="Layout version of this manifest",
    )
    preserved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the most recent preservation attempt on this archive ran",
    )
    identity: PreservedAgentIdentity = Field(description="Who the archive belonged to")
    items: tuple[PreservedItemResult, ...] = Field(description="What each requested item's copy attempt observed")
    transcript_flush: tuple[TranscriptFlushResult, ...] = Field(
        default=(),
        description=(
            "What the final transcript producer passes reported, when any ran. An attempt that "
            "runs no pass keeps what an earlier one recorded, so these can predate preserved_at; "
            "each result carries its own flushed_at."
        ),
    )


class ManifestReadStatus(LowerCaseStrEnum):
    """Why reading an archive's manifest produced, or did not produce, a manifest."""

    LOADED = auto()
    ABSENT = auto()
    UNUSABLE = auto()


class PreservationManifestReadResult(FrozenModel):
    """What reading one archive's manifest found, separating "has none" from "has a bad one"."""

    status: ManifestReadStatus = Field(description="Whether the archive has a manifest, and whether it could be read")
    manifest: PreservationManifest | None = Field(
        default=None, description="The manifest, present exactly when the status is LOADED"
    )


class PreservedAgentArchive(FrozenModel):
    """A discovered preserved archive, including pre-manifest legacy archives."""

    path: Path = Field(description="The archive directory under the local preserved root")
    agent_id: AgentId = Field(description="The archive's agent id, from its manifest or its directory name")
    agent_name: AgentName = Field(description="The archive's agent name, from its manifest or its directory name")
    identity: PreservedAgentIdentity | None = Field(
        default=None, description="Full provenance, absent for a legacy archive that records none"
    )
    manifest: PreservationManifest | None = Field(
        default=None, description="The archive's manifest, absent for a legacy archive"
    )


def get_preserved_agents_root_dir(host_dir: Path) -> Path:
    """Return the directory under which all agents' preserved files are stored.

    This is the single source of truth for where preserved agent data lives on
    disk, so code that needs to enumerate preserved agents (rather than address
    a single one) can do so without duplicating the path structure.

    ``host_dir`` should be the *local* host directory: preserved files always
    live on the local machine so they survive remote host destruction.
    """
    return host_dir / "preserved"


def iter_agent_session_paths(local_host_dir: Path, relpath: Path) -> list[Path]:
    """Return ``<agent_dir>/relpath`` for every live and preserved local agent where it exists.

    Scans both the live agents root (``<host_dir>/agents/``) and the preserved-agents root
    (``<host_dir>/preserved/``); each agent stores its per-agent files under the same
    ``relpath``. Session adoption uses this to find a session id across every local agent's
    native store. The returned paths may be files or directories (``exists()`` is the test),
    so it serves both directory stores (e.g. claude's ``projects/``) and single-file stores
    (e.g. opencode's ``opencode.db``). Local host only: an adopted store is copied onto the
    destination from a path that must already be reachable locally.
    """
    paths: list[Path] = []
    for parent in (get_agents_root_dir(local_host_dir), get_preserved_agents_root_dir(local_host_dir)):
        if not parent.is_dir():
            continue
        for agent_dir in sorted(parent.iterdir()):
            candidate = agent_dir / relpath
            if candidate.exists():
                paths.append(candidate)
    return paths


def dedupe_by_resolved_path(candidates: Iterable[Path]) -> list[Path]:
    """Return ``candidates`` with duplicate paths removed, preserving first-seen order.

    Two candidates are duplicates when they ``resolve()`` to the same real path (so a
    symlinked and a direct route to one dir collapse to one). The original (unresolved)
    path is kept. Session-adoption resolvers use this to dedupe their search dirs -- the
    current/user config dir can coincide with a scanned agent dir -- so a session that
    lives in one physical dir is never reported as ambiguously matching "two" dirs.
    """
    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(candidate)
    return deduped


def run_adopt_session_preflight(
    agent_type: AgentTypeName,
    adopt_session: tuple[str, ...],
    mngr_ctx: MngrContext,
    agent_class: type,
    resolve_one: Callable[[str], object],
) -> None:
    """Fail-fast on bad ``--adopt`` session ids before any host or worktree is created.

    The agent-agnostic gate (the type must support adoption; mutual exclusion with ``--from``)
    runs in :func:`~imbue.mngr.api.create.create`; this is the per-plugin ``on_before_create``
    body. It resolves every named session *now* -- the source is always local, so the result
    matches the resolution done later in ``on_after_provisioning`` -- so a bad id is a clean
    user error rather than a ConcurrencyExceptionGroup traceback out of the provisioning group.

    No-op unless ``adopt_session`` is set and the agent type is (a subtype of) ``agent_class``.
    ``resolve_one`` is the plugin's own resolver, called once per named session for its side
    effect of raising :class:`UserInputError` on an unknown/ambiguous id.
    """
    if not adopt_session:
        return
    resolved = resolve_agent_type(agent_type, mngr_ctx.config)
    # The core gate (`_validate_session_adoption`, which runs before any on_before_create hook)
    # has already rejected `--adopt` for a type that supports no adoption at all, so the resolved
    # type is guaranteed adoption-capable here. A mismatch with *this* plugin's ``agent_class``
    # therefore means the create is for a *different* adoption-capable agent -- whose own hook
    # validates these ids -- not a silent drop. The assert keeps that invariant loud (rather than a
    # silent no-op) if the core gate is ever bypassed or its capability check drifts out of sync.
    if not issubclass(resolved.agent_class, HasSessionAdoptionMixin):
        raise AssertionError(
            f"--adopt reached the {agent_class.__name__} preflight for non-adoption type {agent_type!r}; "
            "_validate_session_adoption should have rejected it first"
        )
    if not issubclass(resolved.agent_class, agent_class):
        return
    for session_arg in adopt_session:
        resolve_one(session_arg)


_MatchT = TypeVar("_MatchT")


def require_unique_match(
    matches: Sequence[_MatchT],
    *,
    not_found_message: str,
    ambiguous_message: str,
) -> _MatchT:
    """Return the single element of ``matches``, raising :class:`UserInputError` for zero or many.

    Every per-CLI adopt resolver scans its native store(s) for a session id and ends the same
    way: zero hits is an unknown-id error (``not_found_message``), more than one is an ambiguity
    (``ambiguous_message`` followed by the colliding candidates, one per indented line), exactly
    one is the answer. Only the store scanning differs per CLI; this shared tail keeps the
    not-found/ambiguous error shape uniform.
    """
    if not matches:
        raise UserInputError(not_found_message)
    if len(matches) > 1:
        listing = "\n".join(f"  {match}" for match in matches)
        raise UserInputError(f"{ambiguous_message}\n{listing}")
    return matches[0]


def adopt_sessions(
    adopt_session: tuple[str, ...],
    source_location: HostLocation | None,
    *,
    copy_explicit: Callable[[str], str],
    copy_clone: Callable[[HostLocation], str | None],
    resume: Callable[[str], None],
) -> None:
    """Copy every ``--adopt`` session (and the ``--from`` clone) into the new agent, then resume one.

    Each ``--adopt`` value is copied in via ``copy_explicit`` (which rebinds it to the new work
    dir and returns its resumable id); a ``--from`` clone is additionally copied via ``copy_clone``.
    The two differ on a *missing* session, by design:

    - ``--adopt`` names a session explicitly, so an unknown/unusable id is a hard error
      (``copy_explicit`` raises ``UserInputError``).
    - ``--from`` is fundamentally a work-dir clone; carrying the session forward is a bonus, so a
      source with no resumable session is a warning, not an error -- ``copy_clone`` returns ``None``.

    The session actually resumed (via ``resume``) is the clone's when ``--from`` yielded one,
    otherwise the last ``--adopt`` value; the rest stay available in the agent's session switcher.
    So ``--adopt A --from X`` resumes X's session, but if X has none it warns and still resumes A.
    With nothing resumable, the agent starts fresh. ``--adopt`` and ``--from`` may be combined.
    """
    resume_id: str | None = None
    for adopt_arg in adopt_session:
        resume_id = copy_explicit(adopt_arg)
    if source_location is not None:
        cloned_id = copy_clone(source_location)
        if cloned_id is not None:
            resume_id = cloned_id
    if resume_id is not None:
        resume(resume_id)


def transfer_cloned_agent_session_store(
    dest_host: OnlineHostInterface,
    dest_state_dir: Path,
    source_location: HostLocation,
    store_relpath: Path,
) -> bool:
    """Copy a cloned source agent's native session store into the destination agent (``--from``).

    A generic ``--from`` clone copies the source *work dir* but not the source agent's
    *state dir*, so an agent that wants the clone to resume the source's conversation
    transfers just its native session store (``store_relpath``, the same relpath it
    preserves and scans) from the source state dir into its own. The agent then rebinds
    that store to its new work_dir. Returns True if the source store existed and was
    copied, else False (the clone starts a fresh session).
    """
    source_store = source_location.path / store_relpath
    if not source_location.host.path_exists(source_store):
        return False
    dest_host.copy_directory(source_location.host, source_store, dest_state_dir / store_relpath)
    return True


def get_preserved_agent_dir(host_dir: Path, agent_name: AgentName, agent_id: AgentId) -> Path:
    """Return the directory under which an agent's preserved files are stored.

    This is the single source of truth for the on-disk layout of preserved
    agent data, so other code (and other plugins) can read those files without
    duplicating the path structure. Preserved files mirror the agent's state
    directory layout underneath this directory.

    ``host_dir`` should be the *local* host directory: preserved files always
    live on the local machine so they survive remote host destruction.
    """
    return get_preserved_agents_root_dir(host_dir) / f"{agent_name}--{agent_id}"


def get_local_preserved_agent_dir(mngr_ctx: MngrContext, agent_name: AgentName, agent_id: AgentId) -> Path:
    """Return the local preserved-files directory for an agent, ignoring cross-host collisions.

    This is the unqualified path, so it names the same directory for two agents that share a
    name and an id on different hosts. Anything *writing* an archive wants
    :func:`get_local_preserved_agent_dir_for_host`, which keeps such a pair apart; this one is
    for reading a path that is already known to be unambiguous.
    """
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    return get_preserved_agent_dir(local_host_dir, agent_name, agent_id)


def discover_preserved_agent_archives(local_host_dir: Path) -> list[PreservedAgentArchive]:
    """Discover manifest-backed and legacy archives under a local host directory."""
    root = get_preserved_agents_root_dir(local_host_dir)
    try:
        is_root_present = root.exists()
    except OSError as e:
        raise MngrError(f"Could not read preserved agent archives under {root}: {e}") from e
    if not is_root_present:
        return []
    try:
        paths = sorted(root.iterdir())
    except NotADirectoryError as e:
        raise MngrError(f"Preserved agent archive root is not a directory: {root}") from e
    except OSError as e:
        raise MngrError(f"Could not read preserved agent archives under {root}: {e}") from e

    archives: list[PreservedAgentArchive] = []
    for path in paths:
        try:
            is_archive_dir = path.is_dir()
        except OSError as e:
            raise MngrError(f"Could not inspect preserved agent archive {path}: {e}") from e
        if not is_archive_dir:
            continue
        result = read_preservation_manifest_result(path, raise_on_io_error=True)
        if result.manifest is not None:
            archives.append(
                PreservedAgentArchive(
                    path=path,
                    agent_id=result.manifest.identity.agent_id,
                    agent_name=result.manifest.identity.agent_name,
                    identity=result.manifest.identity,
                    manifest=result.manifest,
                )
            )
        elif result.status == ManifestReadStatus.ABSENT:
            legacy_identity = _parse_legacy_archive_name(path.name)
            if legacy_identity is not None:
                agent_name, agent_id = legacy_identity
                archives.append(
                    PreservedAgentArchive(
                        path=path,
                        agent_id=agent_id,
                        agent_name=agent_name,
                        identity=_read_legacy_usage_identity(path),
                    )
                )
        else:
            # A manifest that is there but unusable leaves the archive out entirely: falling back
            # to the pre-manifest layout would answer with data the manifest supersedes.
            pass
    return archives


def preserved_agent_matches(archive: PreservedAgentArchive, agent: AgentName | AgentId) -> bool:
    """Match an archive using the address type, preserving ID-vs-name intent."""
    if isinstance(agent, AgentId):
        return archive.agent_id == agent
    return archive.agent_name == agent


# CLEANUP: remove with the rest of the pre-manifest archive support (see _AGENT_DATA_FILENAME).
def _parse_legacy_archive_name(dirname: str) -> tuple[AgentName, AgentId] | None:
    name, separator, id_suffix = dirname.rpartition("--agent-")
    if not separator:
        return None
    agent_id_text = f"agent-{id_suffix.partition('--host-')[0]}"
    try:
        return AgentName(name), AgentId(agent_id_text)
    except ValueError as e:
        logger.trace("Skipping preserved directory {} that names no usable agent: {}", dirname, e)
        return None


# CLEANUP: remove with the rest of the pre-manifest archive support (see _AGENT_DATA_FILENAME).
def _read_legacy_usage_identity(dest_root: Path) -> PreservedAgentIdentity | None:
    """Read identity from the pre-manifest usage sidecars when both are available.

    An archive that keeps no provenance is the one a later destruction is willing to write over,
    so a sidecar that is there but unusable is reported rather than quietly treated as absent.
    """
    data = _read_legacy_json_object(dest_root / _AGENT_DATA_FILENAME)
    meta = _read_legacy_json_object(dest_root / _USAGE_META_SIDECAR_FILENAME)
    if data is None or meta is None:
        return None
    try:
        return PreservedAgentIdentity(
            host_id=HostId(str(meta["host_id"])),
            host_name=HostName(str(meta["host_name"])),
            provider_name=ProviderInstanceName(str(meta["provider_name"])),
            agent_id=AgentId(str(data["id"])),
            agent_name=AgentName(str(data["name"])),
            agent_type=AgentTypeName(str(data["type"])),
            labels=dict(data.get("labels", {})),
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Ignoring unusable legacy preservation metadata in {}: {}", dest_root, e)
        return None


# CLEANUP: remove with the rest of the pre-manifest archive support (see _AGENT_DATA_FILENAME).
def _read_legacy_json_object(path: Path) -> dict[str, Any] | None:
    try:
        content = path.read_text()
    except FileNotFoundError:
        # The ordinary case: an archive preserved by a plugin that writes no usage sidecars.
        logger.debug("No legacy preservation metadata at {}", path)
        return None
    except OSError as e:
        logger.warning("Ignoring unreadable legacy preservation metadata at {}: {}", path, e)
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning("Ignoring corrupt legacy preservation metadata at {}: {}", path, e)
        return None
    if not isinstance(parsed, dict):
        logger.warning("Ignoring legacy preservation metadata at {} that is not a JSON object", path)
        return None
    return parsed


def read_preservation_manifest_result(
    dest_root: Path,
    *,
    # Raise instead of reporting an I/O failure. For a caller walking archive storage itself,
    # where one unreadable path means its whole listing is untrustworthy.
    raise_on_io_error: bool,
) -> PreservationManifestReadResult:
    """Read an archive's manifest, telling an archive that has none from one whose manifest is unusable.

    An unusable manifest is reported rather than raised so one bad archive cannot stop a caller
    walking all of them; the two cases are distinct because only a genuinely absent manifest
    makes a fall back to the pre-manifest archive layout the right answer.
    """
    manifest_path = dest_root / PRESERVATION_MANIFEST_FILENAME
    try:
        content = manifest_path.read_text()
    except FileNotFoundError:
        return PreservationManifestReadResult(status=ManifestReadStatus.ABSENT)
    except OSError as e:
        if raise_on_io_error:
            raise MngrError(f"Could not read preservation manifest {manifest_path}: {e}") from e
        logger.warning("Ignoring unreadable preservation manifest at {}: {}", manifest_path, e)
        return PreservationManifestReadResult(status=ManifestReadStatus.UNUSABLE)
    try:
        manifest = PreservationManifest.model_validate_json(content)
    except ValueError as e:
        logger.warning("Ignoring invalid preservation manifest at {}: {}", manifest_path, e)
        return PreservationManifestReadResult(status=ManifestReadStatus.UNUSABLE)
    return PreservationManifestReadResult(status=ManifestReadStatus.LOADED, manifest=manifest)


def read_preservation_manifest(dest_root: Path) -> PreservationManifest | None:
    """Read an archive's manifest, or None when it has none or the one it has is unusable.

    Use :func:`read_preservation_manifest_result` where the two None cases must be told apart.
    """
    return read_preservation_manifest_result(dest_root, raise_on_io_error=False).manifest


def get_local_preserved_agent_dir_for_host(
    mngr_ctx: MngrContext,
    agent_name: AgentName,
    agent_id: AgentId,
    host_id: HostId,
) -> Path:
    """Choose an archive path without overwriting a known different-host collision.

    The base path includes the agent name and id. Matching origin hosts let
    independent preservation writers contribute to the same agent archive.
    Archives without readable provenance retain the legacy path; only a known
    different host selects the host-qualified destination.

    Falling through on unreadable provenance is deliberate but not free: an
    archive that records no origin host is treated as belonging to whoever is
    writing now, so if it really did come from a different host, that host's
    files stay in the directory and the manifest written afterwards claims all
    of them. Host-qualifying instead would split one agent's archive in two
    whenever a co-writer in the same destroy had not (yet) managed to record
    provenance, and both halves would then answer a lookup by agent name.
    Archives predating the manifest are the population this applies to.
    """
    host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    legacy_path = get_preserved_agent_dir(host_dir, agent_name, agent_id)
    if not legacy_path.exists():
        return legacy_path
    existing = read_preservation_manifest(legacy_path)
    if existing is not None:
        if existing.identity.host_id == host_id:
            return legacy_path
    else:
        legacy_identity = _read_legacy_usage_identity(legacy_path)
        if legacy_identity is None or legacy_identity.host_id == host_id:
            return legacy_path
    return legacy_path.with_name(f"{legacy_path.name}--{host_id}")


def preserve_agent_data(
    items: Sequence[PreservedItem],
    source: HostFileReadInterface,
    agent_state_dir: Path,
    dest_root: Path,
    mngr_ctx: MngrContext,
) -> tuple[PreservedItemResult, ...]:
    """Copy the declared items from ``source`` to ``dest_root``, mirroring layout.

    Each item is read from ``agent_state_dir / item.rel_path`` on ``source`` and
    written to ``dest_root / item.rel_path`` locally. Items that do not exist on
    the source are skipped. Failures for any single item are logged as warnings
    and do not abort the others (or the destruction that triggered this).

    For directories, an online source uses rsync (efficient over SSH); a
    volume-backed offline source walks and copies file-by-file. For single
    files both sources read bytes directly. ``agent_state_dir`` is the absolute
    path of the agent's state directory *as addressed on the source host*. The
    returned result records what this copy attempt observed for every item; it
    does not assert that a producer had flushed complete data before the copy.
    """
    local_host: OnlineHostInterface | None = None
    results: list[PreservedItemResult] = []
    with log_span("Preserving agent data to {}", dest_root):
        for item in items:
            src = agent_state_dir / item.rel_path
            dest = dest_root / item.rel_path
            try:
                if not source.path_exists(src):
                    # Items are usually expected to be present; a debug line helps
                    # diagnose why something did not get preserved.
                    logger.debug("Skipping preservation of {}: not present on source at {}", item.rel_path, src)
                    results.append(_item_result(item, PreservationOutcome.MISSING, None))
                    continue
                if item.kind == FileType.FILE:
                    _write_local_file(dest, source.read_file(src))
                elif isinstance(source, OnlineHostInterface):
                    if local_host is None:
                        local_host = _get_local_online_host(mngr_ctx)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    local_host.copy_directory(source, src, dest)
                else:
                    _copy_tree_via_reader(source, src, dest)
                logger.debug("Preserved {} -> {}", src, dest)
                results.append(_item_result(item, PreservationOutcome.COPIED, None))
            except (MngrError, OSError) as e:
                logger.warning("Failed to preserve {}: {}", item.rel_path, e)
                results.append(_item_result(item, PreservationOutcome.ERROR, _clamp_error(str(e))))
    return tuple(results)


def _clamp_error(error: str) -> str:
    return error[-_PRESERVED_ERROR_CHAR_LIMIT:]


def _item_result(item: PreservedItem, outcome: PreservationOutcome, error: str | None) -> PreservedItemResult:
    return PreservedItemResult(rel_path=item.rel_path, kind=item.kind, outcome=outcome, error=error)


def _write_local_file(dest: Path, content: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)


def _copy_tree_via_reader(source: HostFileReadInterface, src_dir: Path, dest_dir: Path) -> None:
    """Recursively copy a directory tree from a (volume-backed) reader to local disk."""
    for entry in source.list_directory(src_dir, recursive=True):
        # Copy only regular files byte-for-byte. Directories are implied by the recursive
        # walk (their files carry the full relative path); symlinks/devices/pipes/sockets are
        # deliberately not reproduced -- this path copies content, not filesystem structure.
        # A volume-backed offline source only ever yields FILE/DIRECTORY anyway, but checking
        # explicitly for FILE keeps a richer-typed source from silently changing behavior.
        if entry.file_type != FileType.FILE:
            continue
        relative = Path(entry.path).relative_to(src_dir)
        _write_local_file(dest_dir / relative, source.read_file(Path(entry.path)))


def _get_local_online_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Resolve the local host as an OnlineHostInterface (the rsync copy target)."""
    host_interface = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx).get_host(HostName("localhost"))
    if not isinstance(host_interface, OnlineHostInterface):
        raise MngrError("Local host is not online")
    return host_interface


def build_transcript_preserved_items(event_source: str) -> list[PreservedItem]:
    """Return the raw + common transcript directories an agent writes for ``event_source``.

    Every agent plugin follows the same on-disk convention: the raw,
    agent-native transcript lives at ``logs/<event_source>_transcript`` and the
    common (agent-agnostic) transcript at ``events/<event_source>/common_transcript``,
    where ``event_source`` is the agent type's stable source name (e.g. ``codex``,
    ``opencode``, ``pi-coding``, ``antigravity``). A plugin appends its own
    session-id-history :class:`PreservedItem`(s) to this list.
    """
    return [
        PreservedItem(rel_path=f"logs/{event_source}_transcript", kind=FileType.DIRECTORY),
        PreservedItem(rel_path=f"events/{event_source}/common_transcript", kind=FileType.DIRECTORY),
    ]


def flush_agent_transcript(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> tuple[TranscriptFlushResult, ...]:
    """Run bounded single-pass transcript producers before reading their output.

    A successful command only records that the producer exited successfully. It does
    not establish that an external transcript source was complete or that the producer
    recognized every source record.
    """
    if not isinstance(agent, HasTranscriptMixin):
        return ()
    script_names: list[str] = []
    if _RAW_TRANSCRIPT_SCRIPT_NAME in agent.get_raw_transcript_scripts():
        script_names.append(_RAW_TRANSCRIPT_SCRIPT_NAME)
    if (
        isinstance(agent, HasCommonTranscriptMixin)
        and agent.is_common_transcript_enabled
        and _COMMON_TRANSCRIPT_SCRIPT_NAME in agent.get_common_transcript_scripts()
    ):
        script_names.append(_COMMON_TRANSCRIPT_SCRIPT_NAME)
    commands_dir = get_agent_state_dir_path(host.host_dir, agent.id) / "commands"
    return tuple(
        _run_transcript_flush_pass(agent, host, commands_dir / script_name, script_name)
        for script_name in script_names
    )


def _run_transcript_flush_pass(
    agent: AgentInterface,
    host: OnlineHostInterface,
    script_path: Path,
    script_name: str,
) -> TranscriptFlushResult:
    """Run one producer's single pass, reporting rather than raising when it does not deliver."""
    try:
        is_script_present = host.path_exists(script_path)
    except (MngrError, OSError) as e:
        return _failed_transcript_flush(agent.id, script_name, str(e))
    if not is_script_present:
        return TranscriptFlushResult(script_name=script_name, outcome=TranscriptFlushOutcome.SKIPPED)
    command = host.build_source_env_prefix(agent) + (
        f"MNGR_CONVERT_LOCK_TIMEOUT={_FLUSH_CONVERT_LOCK_TIMEOUT_SECONDS} "
        f"bash {shlex.quote(str(script_path))} --single-pass"
    )
    try:
        result = host.execute_idempotent_command(command, timeout_seconds=_FLUSH_COMMAND_TIMEOUT_SECONDS)
    except (MngrError, OSError) as e:
        return _failed_transcript_flush(agent.id, script_name, str(e))
    if result.success:
        return TranscriptFlushResult(script_name=script_name, outcome=TranscriptFlushOutcome.SUCCEEDED)
    return _failed_transcript_flush(agent.id, script_name, result.stderr or result.stdout or "failed")


def _failed_transcript_flush(agent_id: AgentId, script_name: str, error: str) -> TranscriptFlushResult:
    reported = _clamp_error(error)
    logger.warning("Final transcript flush failed for {}: {}: {}", agent_id, script_name, reported)
    return TranscriptFlushResult(script_name=script_name, outcome=TranscriptFlushOutcome.ERROR, error=reported)


def preserve_agent_state(
    items: Sequence[PreservedItem],
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> None:
    """Preserve an online agent's declared items to local storage before its state dir is deleted.

    The whole destruction-time sequence, for use in a plugin's ``on_destroy``, so that the
    plugin only declares *what* to keep: it resolves the agent's state directory on ``host``
    and its local archive destination, runs each transcript producer's final bounded pass so
    the copy sees the freshest output, copies the declared items, and records the attempt --
    the agent's identity, its origin host, and every per-item and per-producer outcome -- in
    the archive's manifest. The caller is responsible for gating on its own
    preserve-on-destroy config flag before calling this.
    """
    dest_root = get_local_preserved_agent_dir_for_host(agent.mngr_ctx, agent.name, agent.id, host.id)
    transcript_flush = flush_agent_transcript(agent, host)
    results = preserve_agent_data(
        items,
        host,
        get_agent_state_dir_path(host.host_dir, agent.id),
        dest_root,
        agent.mngr_ctx,
    )
    identity = PreservedAgentIdentity(
        host_id=host.id,
        host_name=host.get_name(),
        provider_name=host.get_provider_name(),
        agent_id=agent.id,
        agent_name=agent.name,
        agent_type=agent.agent_type,
        labels=agent.get_labels(),
    )
    write_preservation_manifest(
        dest_root,
        PreservationManifest(identity=identity, items=results, transcript_flush=transcript_flush),
    )


def _merge_item_results(
    existing_items: Sequence[PreservedItemResult],
    incoming_items: Sequence[PreservedItemResult],
) -> tuple[PreservedItemResult, ...]:
    """Fold one attempt's per-item outcomes into an archive's, never downgrading a copied item.

    An incoming MISSING says the *source* no longer holds the item, which says nothing about
    the bytes an earlier attempt already copied into the archive -- and consumers (e.g. the
    transcript readers) select archive items by their COPIED outcome, so letting a later,
    less-informed attempt overwrite one would hide data that is still on disk. An incoming
    ERROR does replace a COPIED: a copy that failed part-way may have disturbed the archive.
    """
    merged_by_path = {item.rel_path: item for item in existing_items}
    for item in incoming_items:
        previous = merged_by_path.get(item.rel_path)
        if (
            previous is not None
            and previous.outcome == PreservationOutcome.COPIED
            and item.outcome == PreservationOutcome.MISSING
        ):
            continue
        merged_by_path[item.rel_path] = item
    return tuple(merged_by_path.values())


def _is_attempt_empty(manifest: PreservationManifest) -> bool:
    """Whether this attempt observed nothing worth an archive of its own.

    Nothing was copied, and no producer pass failed. A pass that was skipped or succeeded
    while every item stayed missing leaves an archive with no content to describe; a pass
    that failed is the explanation a reader chasing an absent transcript needs.
    """
    return all(item.outcome == PreservationOutcome.MISSING for item in manifest.items) and all(
        flush.outcome != TranscriptFlushOutcome.ERROR for flush in manifest.transcript_flush
    )


def _is_archive_already_present(dest_root: Path) -> bool:
    try:
        return dest_root.exists()
    except OSError as e:
        # Writing this manifest must not raise, and an archive we cannot even stat is better
        # treated as present: the write that follows reports its own failure.
        logger.warning("Could not check for an existing preserved archive at {}: {}", dest_root, e)
        return True


def write_preservation_manifest(dest_root: Path, manifest: PreservationManifest) -> None:
    """Atomically merge and write a manifest without making destruction depend on it.

    Writing the manifest is what creates the archive directory, so an attempt that found
    nothing at all -- every declared item missing, no producer pass run -- writes nothing
    rather than leaving behind an empty archive that answers later lookups by agent name.
    A failed copy is still recorded: an error is exactly what a reader looking for an
    absent transcript needs to see.
    """
    if _is_attempt_empty(manifest) and not _is_archive_already_present(dest_root):
        logger.debug("Recording no preservation manifest in {}: nothing was preserved", dest_root)
        return
    existing = read_preservation_manifest(dest_root)
    if existing is not None and (
        existing.identity.host_id != manifest.identity.host_id
        or existing.identity.agent_id != manifest.identity.agent_id
    ):
        logger.warning(
            "Refusing to merge preservation manifest for {}@{} into archive for {}@{}",
            manifest.identity.agent_id,
            manifest.identity.host_id,
            existing.identity.agent_id,
            existing.identity.host_id,
        )
        return
    if existing is not None and existing.schema_version > PRESERVATION_MANIFEST_SCHEMA_VERSION:
        logger.warning(
            "Refusing to write a v{} preservation manifest over the v{} one in {}",
            PRESERVATION_MANIFEST_SCHEMA_VERSION,
            existing.schema_version,
            dest_root,
        )
        return
    merged = PreservationManifest(
        preserved_at=manifest.preserved_at,
        identity=manifest.identity,
        items=_merge_item_results(() if existing is None else existing.items, manifest.items),
        transcript_flush=manifest.transcript_flush or (() if existing is None else existing.transcript_flush),
    )
    try:
        atomic_write(dest_root / PRESERVATION_MANIFEST_FILENAME, merged.model_dump_json(indent=2) + "\n")
    except OSError as e:
        logger.warning("Failed to write preservation manifest to {}: {}", dest_root, e)


def flag_gated_items(
    ref: DiscoveredAgent,
    flag_name: str,
    items: Sequence[PreservedItem],
) -> Sequence[PreservedItem] | None:
    """Return ``items`` if the discovered agent opted in via ``flag_name``, else None.

    The shared selector body for a plugin's ``on_before_host_destroy``: it reads
    a boolean preserve-on-destroy flag out of a :class:`DiscoveredAgent`'s
    persisted ``agent_config`` (the raw data.json in ``certified_data``) and
    returns the declared ``items`` to preserve only when that flag is truthy.
    """
    if not ref.certified_data.get("agent_config", {}).get(flag_name):
        return None
    return items


def preserve_host_agents_on_destroy(
    host: HostInterface,
    mngr_ctx: MngrContext,
    agent_type: AgentTypeName,
    # Given a discovered agent (raw data.json in ``certified_data``), return the items to
    # preserve, or None/empty to skip it (e.g. when its preserve-on-destroy flag is off).
    items_for_agent: Callable[[DiscoveredAgent], Sequence[PreservedItem] | None],
) -> None:
    """Preserve declared items for every matching agent on a host about to be destroyed.

    Shared body for a plugin's ``on_before_host_destroy`` hookimpl. When a host
    is destroyed without per-agent ``on_destroy`` calls, agent state still lives
    on the host's persisted volume. If the host exposes that volume (is a
    :class:`HostFileReadInterface`), each agent of ``agent_type`` whose config
    opts in (``items_for_agent`` returns items) is preserved straight off the
    volume via the same :func:`preserve_agent_data` used on the online path. A
    host with no readable volume has nothing to preserve and is skipped.

    Best-effort: a host that cannot be reached to discover its agents is logged
    and skipped, and nothing here may raise, because the hookspec's contract is
    that a raise from ``on_before_host_destroy`` aborts the destroy -- which
    would leave the host most in need of destroying undestroyable.
    """
    if not isinstance(host, HostFileReadInterface):
        logger.debug("Host {} is not readable (no volume); skipping agent preservation", host.id)
        return

    try:
        refs = host.discover_agents()
    except (MngrError, OSError) as e:
        logger.warning("Could not discover agents on host {} to preserve their state: {}", host.id, e)
        return

    for ref in refs:
        if ref.agent_type != agent_type:
            continue
        items = items_for_agent(ref)
        if not items:
            continue
        identity = PreservedAgentIdentity(
            host_id=host.id,
            host_name=host.get_name(),
            provider_name=ref.provider_name,
            agent_id=ref.agent_id,
            agent_name=ref.agent_name,
            # The loop above has already established that this ref carries this type.
            agent_type=agent_type,
            labels=ref.labels,
        )
        dest_root = get_local_preserved_agent_dir_for_host(
            mngr_ctx, identity.agent_name, identity.agent_id, identity.host_id
        )
        results = preserve_agent_data(
            items,
            host,
            get_agent_state_dir_path(host.host_dir, ref.agent_id),
            dest_root,
            mngr_ctx,
        )
        write_preservation_manifest(dest_root, PreservationManifest(identity=identity, items=results))
