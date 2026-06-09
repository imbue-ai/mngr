"""mngr-backed implementation of the documented session functions.

A "session" is a ``robinhood-``-prefixed mngr claude agent created by this SDK (labelled
``created-by=robinhood-agent-sdk``). The functions here are keyed by ``directory`` (the agent's
``cwd``) and operate on claude's *native* session id -- read from the agent's transcript events,
never assumed equal to the mngr agent id, since claude rotates session ids over an agent's life
(compaction, ``/clear``, resume, fork).

* ``list_sessions``        -- enumerate SDK agents for a directory (most-recent first).
* ``get_session_info``     -- one session's ``SDKSessionInfo`` (or ``None`` if unknown).
* ``get_session_messages`` -- the persisted transcript as ``SessionMessage`` objects.
* ``rename_session``       -- set a custom title (stored as a mngr agent label).
* ``tag_session``          -- set/clear a tag (stored as a mngr agent label).

v1 is local-only; cross-cutting create/drive logic lives in :mod:`._agent_sdk.driver`.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Final

from claude_agent_sdk import SDKSessionInfo
from claude_agent_sdk import SessionMessage
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_agent
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr_robinhood._agent_sdk.context import build_sdk_mngr_context
from imbue.mngr_robinhood._agent_sdk.context import open_sdk_concurrency_group
from imbue.mngr_robinhood._agent_sdk.driver import SDK_CREATED_BY_LABEL
from imbue.mngr_robinhood.agent_runtime import destroy_agent
from imbue.mngr_robinhood.errors import RobinhoodError
from imbue.mngr_robinhood.raw_transcript import RAW_TRANSCRIPT_PATH

# Agent-label keys used to persist the documented mutable session metadata.
_CUSTOM_TITLE_LABEL_KEY: Final[str] = "agent-sdk-custom-title"
_TAG_LABEL_KEY: Final[str] = "agent-sdk-tag"

_CREATED_BY_KEY: Final[str] = "created-by"
_SDK_CREATED_BY_VALUE: Final[str] = SDK_CREATED_BY_LABEL[_CREATED_BY_KEY]


class SessionNotFoundError(RobinhoodError, FileNotFoundError):
    """Raised when a session id cannot be resolved to an SDK agent in the given directory."""


class _SessionAgent(FrozenModel):
    """Pairs an SDK agent's metadata with its parsed transcript events (internal helper)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    detail: AgentDetails = Field(description="The agent's listed metadata")
    raw_events: tuple[dict[str, Any], ...] = Field(description="The agent's parsed transcript events")

    @property
    def session_ids(self) -> list[str]:
        ids: list[str] = []
        for event in self.raw_events:
            session_id = event.get("sessionId")
            if isinstance(session_id, str) and session_id and session_id not in ids:
                ids.append(session_id)
        return ids

    @property
    def primary_session_id(self) -> str | None:
        ids = self.session_ids
        return ids[-1] if ids else None


def _build_mngr_ctx() -> tuple[MngrContext, ConcurrencyGroup]:
    concurrency_group = open_sdk_concurrency_group()
    return build_sdk_mngr_context(concurrency_group), concurrency_group


def _parse_raw_events(content: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed session transcript line: {}", exc)
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _read_agent_raw_events(mngr_ctx: MngrContext, detail: AgentDetails) -> list[dict[str, Any]]:
    events_target: EventsTarget | None = try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=detail.id,
        agent_name=str(detail.name),
        host_id=detail.host.id,
        provider_name=LOCAL_PROVIDER_NAME,
    )
    if events_target is None:
        return []
    try:
        content = read_event_content(events_target, RAW_TRANSCRIPT_PATH)
    except MngrError as exc:
        if "No such file or directory" not in str(exc):
            logger.warning("Failed to read session transcript for {}: {}", detail.name, exc)
        return []
    return _parse_raw_events(content)


def _list_sdk_session_agents(mngr_ctx: MngrContext, directory: str | None) -> list[_SessionAgent]:
    """Return SDK agents whose cwd matches ``directory``, each paired with its transcript events."""
    result = list_agents(mngr_ctx, is_streaming=False)
    target_dir = Path(directory).resolve() if directory is not None else Path.cwd().resolve()
    session_agents: list[_SessionAgent] = []
    for detail in result.agents:
        if detail.labels.get(_CREATED_BY_KEY) != _SDK_CREATED_BY_VALUE:
            continue
        if Path(detail.work_dir).resolve() != target_dir:
            continue
        raw_events = _read_agent_raw_events(mngr_ctx, detail)
        session_agents.append(_SessionAgent(detail=detail, raw_events=tuple(raw_events)))
    return session_agents


def _epoch_seconds(timestamp: str | None) -> int | None:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        normalized = timestamp.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def _first_user_prompt(raw_events: Sequence[Mapping[str, Any]]) -> str | None:
    for event in raw_events:
        if event.get("type") != "user" or bool(event.get("isMeta", False)):
            continue
        message = event.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str) and content:
                return content
    return None


def _event_timestamps(raw_events: Sequence[Mapping[str, Any]]) -> list[int]:
    stamps: list[int] = []
    for event in raw_events:
        epoch = _epoch_seconds(event.get("timestamp") if isinstance(event.get("timestamp"), str) else None)
        if epoch is not None:
            stamps.append(epoch)
    return stamps


def _build_session_info(session_agent: _SessionAgent, session_id: str) -> SDKSessionInfo:
    detail = session_agent.detail
    raw_events = session_agent.raw_events
    timestamps = _event_timestamps(raw_events)
    last_modified = max(timestamps) if timestamps else int(detail.create_time.timestamp())
    created_at = min(timestamps) if timestamps else int(detail.create_time.timestamp())
    first_prompt = _first_user_prompt(raw_events)
    custom_title = detail.labels.get(_CUSTOM_TITLE_LABEL_KEY)
    tag = detail.labels.get(_TAG_LABEL_KEY)
    summary = first_prompt or str(detail.name)
    return SDKSessionInfo(
        session_id=session_id,
        summary=summary,
        last_modified=last_modified,
        file_size=None,
        custom_title=custom_title,
        first_prompt=first_prompt,
        git_branch=detail.initial_branch,
        cwd=str(Path(detail.work_dir).resolve()),
        tag=tag,
        created_at=created_at,
    )


def list_sessions(
    directory: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    include_worktrees: bool = True,
) -> list[SDKSessionInfo]:
    """List SDK sessions for ``directory``, most-recently-modified first, with ``limit``/``offset``."""
    mngr_ctx, concurrency_group = _build_mngr_ctx()
    try:
        session_agents = _list_sdk_session_agents(mngr_ctx, directory)
    finally:
        concurrency_group.__exit__(None, None, None)
    infos: list[SDKSessionInfo] = []
    for session_agent in session_agents:
        session_id = session_agent.primary_session_id
        if session_id is not None:
            infos.append(_build_session_info(session_agent, session_id))
    infos.sort(key=lambda info: info.last_modified, reverse=True)
    start = offset if offset is not None else 0
    sliced = infos[start:]
    if limit is not None:
        sliced = sliced[:limit]
    return sliced


def _find_session_agent(mngr_ctx: MngrContext, directory: str | None, session_id: str) -> _SessionAgent | None:
    for session_agent in _list_sdk_session_agents(mngr_ctx, directory):
        if session_id in session_agent.session_ids:
            return session_agent
    return None


def get_session_info(session_id: str, directory: str | None = None) -> SDKSessionInfo | None:
    """Return one session's info, or ``None`` if no matching SDK session exists in ``directory``."""
    mngr_ctx, concurrency_group = _build_mngr_ctx()
    try:
        session_agent = _find_session_agent(mngr_ctx, directory, session_id)
    finally:
        concurrency_group.__exit__(None, None, None)
    if session_agent is None:
        return None
    return _build_session_info(session_agent, session_id)


def _build_session_message(raw_event: Mapping[str, Any], session_id: str) -> SessionMessage | None:
    event_type = raw_event.get("type")
    if event_type not in ("user", "assistant") or bool(raw_event.get("isMeta", False)):
        return None
    uuid = raw_event.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    return SessionMessage(
        type=event_type,
        uuid=uuid,
        session_id=session_id,
        message=raw_event.get("message"),
    )


def get_session_messages(
    session_id: str,
    directory: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[SessionMessage]:
    """Return the session's persisted transcript as ``SessionMessage`` objects (``[]`` if unknown)."""
    mngr_ctx, concurrency_group = _build_mngr_ctx()
    try:
        session_agent = _find_session_agent(mngr_ctx, directory, session_id)
    finally:
        concurrency_group.__exit__(None, None, None)
    if session_agent is None:
        return []
    messages: list[SessionMessage] = []
    for raw_event in session_agent.raw_events:
        if raw_event.get("sessionId") != session_id:
            continue
        message = _build_session_message(raw_event, session_id)
        if message is not None:
            messages.append(message)
    start = offset if offset is not None else 0
    sliced = messages[start:]
    if limit is not None:
        sliced = sliced[:limit]
    return sliced


def _set_session_label(session_id: str, directory: str | None, key: str, value: str | None) -> None:
    """Set or clear one label on the agent that owns ``session_id``; raise if no such session."""
    mngr_ctx, concurrency_group = _build_mngr_ctx()
    try:
        session_agent = _find_session_agent(mngr_ctx, directory, session_id)
        if session_agent is None:
            raise SessionNotFoundError(f"No SDK session {session_id!r} found in directory {directory!r}")
        agent = _resolve_live_agent(mngr_ctx, session_agent.detail)
        updated_labels = dict(agent.get_labels())
        if value is None:
            updated_labels.pop(key, None)
        else:
            updated_labels[key] = value
        agent.set_labels(updated_labels)
    finally:
        concurrency_group.__exit__(None, None, None)


def _resolve_live_agent_and_host(
    mngr_ctx: MngrContext, detail: AgentDetails
) -> tuple[AgentInterface, OnlineHostInterface]:
    host_ref, agent_ref = find_one_agent(detail.address, mngr_ctx)
    return resolve_to_started_host_and_agent(host_ref, agent_ref, allow_auto_start=False, mngr_ctx=mngr_ctx)


def _resolve_live_agent(mngr_ctx: MngrContext, detail: AgentDetails) -> AgentInterface:
    agent, _host = _resolve_live_agent_and_host(mngr_ctx, detail)
    return agent


def rename_session(session_id: str, title: str, directory: str | None = None) -> None:
    """Set a session's custom title (stored as a mngr agent label). Raises if the session is unknown."""
    _set_session_label(session_id, directory, _CUSTOM_TITLE_LABEL_KEY, title)


def tag_session(session_id: str, tag: str | None, directory: str | None = None) -> None:
    """Set or clear a session's tag (stored as a mngr agent label). Raises if the session is unknown."""
    _set_session_label(session_id, directory, _TAG_LABEL_KEY, tag)


def destroy_sessions_in_directory(directory: str | None = None) -> None:
    """Destroy every SDK session (mngr agent) in ``directory``. Best-effort cleanup utility.

    Intended for test teardown and explicit housekeeping; the normal SDK lifecycle only *stops*
    agents (leaving sessions readable), so leaked sessions accumulate without an explicit sweep.
    """
    mngr_ctx, concurrency_group = _build_mngr_ctx()
    try:
        for session_agent in _list_sdk_session_agents(mngr_ctx, directory):
            try:
                agent, host = _resolve_live_agent_and_host(mngr_ctx, session_agent.detail)
            except (MngrError, RuntimeError) as exc:
                logger.warning("Failed to resolve SDK agent {} for cleanup: {}", session_agent.detail.name, exc)
                continue
            destroy_agent(agent, host)
    finally:
        concurrency_group.__exit__(None, None, None)
