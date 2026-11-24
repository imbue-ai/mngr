"""Derivation of the in-flight / interrupted / failed create attempt rows.

Every workspace create attempt appears as a row in the workspace list from the
moment it starts: live create attempts from the in-memory ``AgentCreator`` registry,
plus record-backed rows for create attempts a previous app session left behind
(interrupted by a restart, or failed with a persisted error snapshot). The
row hands off to the real workspace row in place: a create attempt whose canonical
agent id has appeared in a discovery snapshot stops producing a row (and the
discovery sweep deletes its record), so there is no flicker between "creating"
and "created".

This module is a pure derivation over the two sources; the payload builders in
``app.py`` (chrome SSE + landing page) and the ``/creating/<id>`` page route
consume it.
"""

from collections.abc import Sequence
from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptInfo
from imbue.minds.desktop_client.agent_creator import AgentCreateAttemptStatus
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.primitives import LaunchMode


class CreateAttemptRowKind(UpperCaseStrEnum):
    """Which badge a create attempt row wears in the workspace list."""

    # A create attempt is running in this process (or finished and is awaiting its
    # first discovery confirmation, so the row can hand off without flicker).
    CREATING = auto()
    # A record-backed create attempt with no live thread behind it: an app restart
    # killed the create mid-flight. Offers retry (pre-filled form) + discard.
    INTERRUPTED = auto()
    # The create attempt failed; the error (and, across restarts, the persisted log
    # tail) stays viewable until the row is dismissed.
    FAILED = auto()


class CreateAttemptRow(FrozenModel):
    """One workspace-list row for a create attempt that has not handed off to a workspace yet."""

    create_attempt_id: str = Field(description="The create attempt id; also the row's stable identity in the list")
    kind: CreateAttemptRowKind = Field(description="Badge / affordance the row carries")
    display_name: str = Field(description="Human-readable workspace name shown on the row")
    host_name: str = Field(description="Resolved host-name slug the create attempt targets")
    provider_instance_name: str = Field(default="", description="Provider instance the create attempt targets")
    launch_mode: LaunchMode = Field(description="Compute launch mode of the create attempt")
    account_email: str = Field(default="", description="Owning account's email, empty for a private workspace")
    account_id: str = Field(default="", description="Owning account's user id, empty for a private workspace")
    color: str | None = Field(default=None, description="Workspace accent color, when the request chose one")
    error: str | None = Field(default=None, description="Error message, set for FAILED rows")
    error_kind: str | None = Field(default=None, description="Machine-readable failure classification, if any")


class CreateAttemptOwner(FrozenModel):
    """The account a create attempt was submitted under, for the live row its workspace shows before its association lands."""

    account_id: str = Field(description="Owning account's user id")
    account_email: str = Field(default="", description="Owning account's email, when the record carries one")


@pure
def derive_settling_create_owners(
    live_infos: Sequence[AgentCreateAttemptInfo],
    records: Sequence[PendingCreateAttemptRecord],
) -> dict[str, CreateAttemptOwner]:
    """The owner of every workspace whose create has not failed, by canonical agent id.

    From the moment ``mngr create`` returns until the create's completion
    writes its account association, the workspace is in discovery (a live
    row) with no association, so the row would read as the device's for the
    whole boot wait. The create attempt's record names the account the row is
    really for. A DONE attempt stays in the map: DONE is published before the
    association is written, and once that lands the association is read first.
    """
    record_by_create_attempt_id = {record.create_attempt_id: record for record in records}
    owners: dict[str, CreateAttemptOwner] = {}
    for info in live_infos:
        if info.agent_id is None or info.status is AgentCreateAttemptStatus.FAILED:
            continue
        record = record_by_create_attempt_id.get(str(info.create_attempt_id))
        if record is None or not record.request.account_id:
            continue
        owners[str(info.agent_id)] = CreateAttemptOwner(
            account_id=record.request.account_id, account_email=record.request.account_email
        )
    return owners


def _row_from_live_info(
    info: AgentCreateAttemptInfo, record: PendingCreateAttemptRecord | None
) -> CreateAttemptRow | None:
    display_name = record.request.display_name if record is not None and record.request.display_name else ""
    if info.status is AgentCreateAttemptStatus.DONE:
        # DONE with a still-present record means discovery has not confirmed
        # the workspace yet: keep the row so the list never blinks empty
        # between "creating" and the real workspace row. With no record left
        # (or no record store at all) there is nothing awaiting confirmation.
        if record is None:
            return None
        kind = CreateAttemptRowKind.CREATING
    elif info.status is AgentCreateAttemptStatus.FAILED:
        kind = CreateAttemptRowKind.FAILED
    else:
        kind = CreateAttemptRowKind.CREATING
    return CreateAttemptRow(
        create_attempt_id=str(info.create_attempt_id),
        kind=kind,
        display_name=display_name or info.host_name or str(info.create_attempt_id),
        host_name=info.host_name,
        provider_instance_name=info.provider_instance_name,
        launch_mode=info.launch_mode,
        account_email=record.request.account_email if record is not None else "",
        account_id=record.request.account_id if record is not None else "",
        color=record.request.color if record is not None else None,
        error=info.error,
        error_kind=str(info.error_kind) if info.error_kind is not None else None,
    )


def _row_from_record(record: PendingCreateAttemptRecord) -> CreateAttemptRow | None:
    if record.state is PendingCreateAttemptState.DONE:
        # The create finished (possibly in a previous session); the workspace
        # exists and the startup reconcile / discovery sweep own the record's
        # remaining lifecycle. No create attempt row.
        return None
    kind = (
        CreateAttemptRowKind.FAILED
        if record.state is PendingCreateAttemptState.FAILED
        else CreateAttemptRowKind.INTERRUPTED
    )
    return CreateAttemptRow(
        create_attempt_id=record.create_attempt_id,
        kind=kind,
        display_name=record.request.display_name or record.request.host_name,
        host_name=record.request.host_name,
        provider_instance_name=record.provider_instance_name,
        launch_mode=record.request.launch_mode,
        account_email=record.request.account_email,
        account_id=record.request.account_id,
        color=record.request.color,
        error=record.error,
        error_kind=record.error_kind,
    )


@pure
def is_host_being_created(rows: Sequence[CreateAttemptRow], provider_instance_name: str, host_name: str) -> bool:
    """Whether a CREATING row targets ``host_name`` on ``provider_instance_name`` (names compare casefolded)."""
    folded_host_name = host_name.casefold()
    return any(
        row.kind is CreateAttemptRowKind.CREATING
        and row.provider_instance_name == provider_instance_name
        and row.host_name.casefold() == folded_host_name
        for row in rows
    )


class OwnerActiveWorkspace(FrozenModel):
    """One active workspace this install associates with a create attempt's owner."""

    agent_id: str = Field(description="The workspace's agent id")
    provider_instance_name: str = Field(description="Provider instance the workspace's host lives under, if known")
    host_name: str = Field(description="The workspace's host name, if known")


@pure
def is_first_workspace_of_owner(
    owner_active_workspaces: Sequence[OwnerActiveWorkspace],
    attempt_agent_id: str | None,
    attempt_provider_instance_name: str,
    attempt_host_name: str,
) -> bool:
    """Whether a create attempt's owner has no active workspace other than the attempt's own.

    The attempt's own workspace is the one with its agent id. Until ``mngr create`` returns
    that id the workspace can already be listed (the connector writes a cloud record at lease
    time, and a failed attempt may leave its host behind), so it is then recognized by the
    host the attempt targets, as ``is_host_being_created`` does.
    """
    folded_host_name = attempt_host_name.casefold()
    for workspace in owner_active_workspaces:
        if attempt_agent_id is not None:
            is_own = workspace.agent_id == attempt_agent_id
        else:
            is_own = (
                workspace.provider_instance_name == attempt_provider_instance_name
                and workspace.host_name.casefold() == folded_host_name
            )
        if not is_own:
            return False
    return True


@pure
def derive_create_attempt_rows(
    live_infos: Sequence[AgentCreateAttemptInfo],
    records: Sequence[PendingCreateAttemptRecord],
    known_agent_id_strs: frozenset[str],
) -> list[CreateAttemptRow]:
    """Merge live create attempts and pending records into the visible create attempt rows.

    A create attempt appears at most once: the live in-memory view wins over its
    record (fresher status), and any create attempt whose canonical agent id already
    appears in ``known_agent_id_strs`` (a discovery snapshot) produces no row
    at all -- the real workspace row has taken over.
    """
    record_by_create_attempt_id = {record.create_attempt_id: record for record in records}
    live_create_attempt_ids = {str(info.create_attempt_id) for info in live_infos}

    rows: list[CreateAttemptRow] = []
    for info in live_infos:
        if info.agent_id is not None and str(info.agent_id) in known_agent_id_strs:
            continue
        row = _row_from_live_info(info, record_by_create_attempt_id.get(str(info.create_attempt_id)))
        if row is not None:
            rows.append(row)

    # Record-only rows (no live create attempt behind them), oldest first so the
    # list order is stable across rebuilds.
    leftover_records = sorted(
        (record for record in records if record.create_attempt_id not in live_create_attempt_ids),
        key=lambda record: record.created_at,
    )
    for record in leftover_records:
        if record.agent_id is not None and record.agent_id in known_agent_id_strs:
            continue
        row = _row_from_record(record)
        if row is not None:
            rows.append(row)
    return rows
