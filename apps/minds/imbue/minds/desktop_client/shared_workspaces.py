"""The shared-workspaces page: every share an account holds, joined to the account's workspace records.

A share lives at the connector and counts against ``max_shared_workspaces``
until it is retired, which the workspace's destroy normally does. One that
survived its workspace (a local docker workspace an old computer was sharing,
a destroy that never reached the connector) is a leaked slot that only its
owner can free: the connector pairs shares with workspace records and retires
the ones whose record is destroyed, but a share with no record at all is left
to the user. This module builds the rows that page shows, decides what
removing one does, and clears the materials of a share that another device or
the connector retired while this device still hosted its workspace.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Final

from loguru import logger

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.api_models import SharedWorkspaceRow
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.data_types import ShareRemovalOutcome
from imbue.minds.desktop_client.data_types import SharedWorkspaceRecordState
from imbue.minds.desktop_client.forward_identity import ForwardIdentityPublisher
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_materials_injection import clear_share_publication_from_agent
from imbue.minds.desktop_client.sharing_handler import LISTED_SHARE_STATES
from imbue.minds.desktop_client.sharing_handler import SharingError
from imbue.minds.desktop_client.sharing_handler import describe_connector_failure
from imbue.minds.desktop_client.sharing_handler import unpublish_workspace
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.primitives import AgentId

SHARED_WORKSPACES_PAGE_PATH: Final[str] = "/workspaces/shared"

# A live tunnel stamps the share at most once per fifteen minutes (the
# connector's SHARE_LAST_SEEN_WRITE_INTERVAL); a stamp younger than this reads
# as connected now.
_CONNECTED_NOW_WINDOW: Final[timedelta] = timedelta(minutes=20)

_HOURS_PER_DAY: Final[int] = 24


@pure
def _parse_connector_timestamp(value: str | None) -> datetime | None:
    """A connector timestamp (``str(datetime)``, space-separated) as an aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@pure
def describe_last_seen(last_seen_at: str | None, now: datetime) -> str:
    """Say when the share's tunnel last proved itself alive, the way the page shows it."""
    seen_at = _parse_connector_timestamp(last_seen_at)
    if seen_at is None:
        return "Never connected"
    age = now - seen_at
    if age < _CONNECTED_NOW_WINDOW:
        return "Connected now"
    hours = max(int(age.total_seconds() // 3600), 1)
    if hours < _HOURS_PER_DAY:
        return f"Last connected {hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // _HOURS_PER_DAY
    return f"Last connected {days} day{'' if days == 1 else 's'} ago"


@pure
def _record_for_share(share: ShareCliInfo, records: Sequence[ReplicaRecord]) -> ReplicaRecord | None:
    """Pair a share with its workspace's record the way the connector does.

    A workspace-keyed share pairs by workspace id. A legacy row (no workspace
    id) pairs through the host it names; an active record wins over a
    destroyed one there, since a host that held a destroyed workspace before
    its current one is named by both.
    """
    if share.workspace_id is not None:
        return next((record for record in records if record.agent_id == share.workspace_id), None)
    on_host = [record for record in records if record.host_id == share.host_id]
    active = [record for record in on_host if record.state == RECORD_STATE_ACTIVE]
    return active[0] if active else (on_host[0] if on_host else None)


@pure
def build_shared_workspace_rows(
    shares: Sequence[ShareCliInfo],
    records: Sequence[ReplicaRecord],
    account_email: str,
    # The workspaces this device currently discovers, by agent id.
    hosted_here_agent_ids: AbstractSet[str],
    now: datetime,
) -> list[SharedWorkspaceRow]:
    """The page's rows for one account: its slot-holding shares, each joined to its workspace record."""
    rows: list[SharedWorkspaceRow] = []
    for share in shares:
        if share.state not in LISTED_SHARE_STATES:
            continue
        record = _record_for_share(share, records)
        if record is None:
            record_state = SharedWorkspaceRecordState.MISSING
        elif record.state == RECORD_STATE_ACTIVE:
            record_state = SharedWorkspaceRecordState.ACTIVE
        else:
            record_state = SharedWorkspaceRecordState.DESTROYED
        workspace_id = share.workspace_id or (record.agent_id if record is not None else None)
        is_hosted_here = workspace_id is not None and workspace_id in hosted_here_agent_ids
        created_at = _parse_connector_timestamp(share.created_at)
        rows.append(
            SharedWorkspaceRow(
                host_id=share.host_id,
                workspace_id=workspace_id,
                account_email=account_email,
                display_name=(record.display_name if record is not None and record.display_name else "")
                or share.workspace_domain,
                workspace_domain=share.workspace_domain,
                device_label="This device"
                if is_hosted_here
                else (record.device_label if record is not None and record.device_label else "Unknown device"),
                share_state=share.state,
                record_state=record_state,
                created_display=created_at.strftime("%Y-%m-%d") if created_at is not None else "",
                last_seen_display=describe_last_seen(share.last_seen_at, now),
                is_likely_leaked=record_state is not SharedWorkspaceRecordState.ACTIVE,
                is_hosted_here=is_hosted_here,
            )
        )
    return rows


def hosted_host_ids_by_agent_id(backend_resolver: BackendResolverInterface) -> dict[str, str]:
    """The workspaces this device currently discovers on a ``host-`` coordinate, each with its host id."""
    hosted: dict[str, str] = {}
    for agent_id in backend_resolver.list_active_workspace_ids():
        display_info = backend_resolver.get_agent_display_info(agent_id)
        if display_info is not None and str(display_info.host_id).startswith("host-"):
            hosted[str(agent_id)] = str(display_info.host_id)
    return hosted


def list_account_shared_workspaces(
    account_email: str,
    user_id: str,
    cli: ImbueCloudCli,
    session_store: MultiAccountSessionStore,
    backend_resolver: BackendResolverInterface,
) -> list[SharedWorkspaceRow]:
    """The page's rows for one signed-in account. Raises :class:`SharingError` when the connector cannot be read."""
    try:
        shares = cli.list_shares(account=account_email)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not list this account's shares: {describe_connector_failure(exc)}") from exc
    records = session_store.record_store.list_records(user_id) if session_store.record_store is not None else []
    return build_shared_workspace_rows(
        shares,
        records,
        account_email,
        hosted_host_ids_by_agent_id(backend_resolver).keys(),
        datetime.now(timezone.utc),
    )


def remove_account_share(
    account_email: str,
    host_id: str,
    cli: ImbueCloudCli,
    session_store: MultiAccountSessionStore,
    backend_resolver: BackendResolverInterface,
    forward_identity: ForwardIdentityPublisher | None,
) -> ShareRemovalOutcome:
    """Free the share's slot: unpublish a workspace this device runs, else delete the share at the connector.

    The unpublish path is taken only for a hosted workspace this account owns:
    an unpublish acts for the workspace's own account, so for any other hosted
    workspace it would leave this account's share holding the slot.

    Raises :class:`SharingError` when the connector refuses or cannot be
    reached. Idempotent: a share already gone is a success.
    """
    if _is_hosted_here_by_account(host_id, account_email, session_store, backend_resolver):
        unpublish_workspace(host_id, backend_resolver, cli, session_store, forward_identity)
        return ShareRemovalOutcome.STOPPED_SHARING
    _delete_share_at_connector(account_email, host_id, cli, forward_identity)
    return ShareRemovalOutcome.REMOVED


def _is_hosted_here_by_account(
    host_id: str,
    account_email: str,
    session_store: MultiAccountSessionStore,
    backend_resolver: BackendResolverInterface,
) -> bool:
    """Whether this device runs the workspace on ``host_id`` and it belongs to the account ``account_email``."""
    for agent_id, hosted_host_id in hosted_host_ids_by_agent_id(backend_resolver).items():
        if hosted_host_id == host_id:
            owner = session_store.get_account_for_workspace(agent_id)
            return owner is not None and str(owner.email) == account_email
    return False


def _delete_share_at_connector(
    account_email: str, host_id: str, cli: ImbueCloudCli, forward_identity: ForwardIdentityPublisher | None
) -> None:
    """Delete a share the connector still lists as holding a slot; one already gone needs nothing."""
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not read this share's status: {describe_connector_failure(exc)}") from exc
    if share is None or share.state not in LISTED_SHARE_STATES:
        return
    try:
        cli.delete_share(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not remove this share: {describe_connector_failure(exc)}") from exc
    if forward_identity is not None and share.workspace_id is not None:
        forward_identity.mark_unshared(share.workspace_id)
        forward_identity.request_sync()


def clear_materials_of_shares_retired_elsewhere(
    no_longer_shared_by_user_id: Mapping[str, Sequence[str]],
    cli: ImbueCloudCli,
    session_store: MultiAccountSessionStore,
    backend_resolver: BackendResolverInterface,
) -> list[str]:
    """Take the share materials out of the workspaces this device hosts whose share was retired elsewhere.

    A share removed from another device, or retired by the connector, leaves
    the hosting workspace with a ``share.env`` whose relay token is dead: its
    gateway would retry the tunnel forever and the relay would refuse every
    attempt. The input is what one sync pass found no longer shared since the
    pass before it, so only a retirement that lands while the app runs reaches
    here (the comparison starts from an empty set at launch). The connector
    is asked before anything is removed, so a sync pass that listed the
    shares before a publish here landed cannot strip a share that does exist.
    Returns the agent ids whose materials were cleared. Never raises: this
    runs inside the sync pass.
    """
    hosted = hosted_host_ids_by_agent_id(backend_resolver)
    cleared: list[str] = []
    for user_id, agent_ids in no_longer_shared_by_user_id.items():
        account_email = session_store.get_account_email(user_id)
        if account_email is None:
            continue
        for agent_id in agent_ids:
            if agent_id not in hosted:
                continue
            if _clear_if_connector_confirms_unshared(agent_id, hosted[agent_id], account_email, cli, backend_resolver):
                cleared.append(agent_id)
    return cleared


def _clear_if_connector_confirms_unshared(
    agent_id: str,
    host_id: str,
    account_email: str,
    cli: ImbueCloudCli,
    backend_resolver: BackendResolverInterface,
) -> bool:
    try:
        parsed_agent_id = AgentId(agent_id)
    except InvalidRandomIdError:
        logger.warning(
            "Leaving the share materials of {} in place: not a workspace id this device can address", agent_id
        )
        return False
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        logger.warning("Leaving the share materials of {} in place: {}", agent_id, describe_connector_failure(exc))
        return False
    if share is not None and share.state in LISTED_SHARE_STATES:
        return False
    if not clear_share_publication_from_agent(build_agent_address(parsed_agent_id, backend_resolver), cli.mngr_caller):
        return False
    logger.info("Cleared the share materials of {}: its share was retired elsewhere", agent_id)
    return True
