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
"""

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

from loguru import logger
from pydantic import Field

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
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME


class PreservedItem(FrozenModel):
    """One file or directory to preserve, addressed relative to the agent state dir."""

    rel_path: str = Field(description="Path relative to the agent state directory")
    kind: FileType = Field(description="Whether rel_path is a FILE or a DIRECTORY")


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
    - ``--from`` is fundamentally a workspace clone; carrying the session forward is a bonus, so a
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

    A generic ``--from`` clone copies the source *workspace* but not the source agent's
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
    """Return the local preserved-files directory for an agent."""
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    return get_preserved_agent_dir(local_host_dir, agent_name, agent_id)


def preserve_agent_data(
    items: Sequence[PreservedItem],
    source: HostFileReadInterface,
    agent_state_dir: Path,
    dest_root: Path,
    mngr_ctx: MngrContext,
) -> None:
    """Copy the declared items from ``source`` to ``dest_root``, mirroring layout.

    Each item is read from ``agent_state_dir / item.rel_path`` on ``source`` and
    written to ``dest_root / item.rel_path`` locally. Items that do not exist on
    the source are skipped. Failures for any single item are logged as warnings
    and do not abort the others (or the destruction that triggered this).

    For directories, an online source uses rsync (efficient over SSH); a
    volume-backed offline source walks and copies file-by-file. For single
    files both sources read bytes directly. ``agent_state_dir`` is the absolute
    path of the agent's state directory *as addressed on the source host*.
    """
    local_host: OnlineHostInterface | None = None
    with log_span("Preserving agent data to {}", dest_root):
        for item in items:
            src = agent_state_dir / item.rel_path
            dest = dest_root / item.rel_path
            try:
                if not source.path_exists(src):
                    # Items are usually expected to be present; a debug line helps
                    # diagnose why something did not get preserved.
                    logger.debug("Skipping preservation of {}: not present on source at {}", item.rel_path, src)
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
            except (MngrError, OSError) as e:
                logger.warning("Failed to preserve {}: {}", item.rel_path, e)


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


def preserve_agent_state(
    items: Sequence[PreservedItem],
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> None:
    """Preserve an online agent's declared items to local storage before its state dir is deleted.

    Thin wrapper over :func:`preserve_agent_data` for use in a plugin's
    ``on_destroy``: it resolves the agent's state directory on ``host`` and the
    agent's local preserved-files destination, so the plugin only declares
    *what* to keep. The caller is responsible for gating on its own
    preserve-on-destroy config flag before calling this.
    """
    preserve_agent_data(
        items,
        host,
        get_agent_state_dir_path(host.host_dir, agent.id),
        get_local_preserved_agent_dir(agent.mngr_ctx, agent.name, agent.id),
        agent.mngr_ctx,
    )


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
    """
    if not isinstance(host, HostFileReadInterface):
        logger.debug("Host {} is not readable (no volume); skipping agent preservation", host.id)
        return

    for ref in host.discover_agents():
        if ref.agent_type != agent_type:
            continue
        items = items_for_agent(ref)
        if not items:
            continue
        preserve_agent_data(
            items,
            host,
            get_agent_state_dir_path(host.host_dir, ref.agent_id),
            get_local_preserved_agent_dir(mngr_ctx, ref.agent_name, ref.agent_id),
            mngr_ctx,
        )
