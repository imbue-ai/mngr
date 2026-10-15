from collections.abc import Callable
from collections.abc import Set as AbstractSet
from typing import Final

from loguru import logger

from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.data_types import SignedInAccount
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.provider_settings import ProviderSettingsMaterializer
from imbue.minds.desktop_client.dek_store import is_account_unlocked
from imbue.minds.desktop_client.dek_store import is_master_password_set_for_account
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.ui_models import AccountPanelState
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore


def list_signed_in_accounts_from_session_store(
    session_store: MultiAccountSessionStore,
) -> list[SignedInAccount] | None:
    """The materializer's account source after startup: the plugin's session list, or None when the listing failed.

    A failed account listing is not an empty one: materializing it would delete
    every account and connection block, so the materializer leaves the settings
    as they are and the next trigger writes them.
    """
    listed_accounts = session_store.list_accounts()
    if session_store.is_last_identity_read_failed:
        return None
    return [SignedInAccount(user_id=str(account.user_id), email=str(account.email)) for account in listed_accounts]


CREATE_IN_FLIGHT_ON_ACCOUNT_MESSAGE: Final[str] = (
    "A workspace is still being created on this account's providers; wait for it to finish, then sign out."
)


def account_provider_instance_names(user_id: str, connection_store: ConnectionStore | None) -> set[str]:
    """The account's own provider instances: its imbue_cloud instance plus every active connection's instance."""
    names = {imbue_cloud_provider_name_for_user(user_id)}
    if connection_store is not None:
        for record in connection_store.list_active_connections(user_id):
            instance_name = connection_store.instance_name_for(record)
            if instance_name is not None:
                names.add(instance_name)
    return names


def has_live_create_on_account(
    user_id: str, connection_store: ConnectionStore | None, in_flight_provider_names: AbstractSet[str]
) -> bool:
    """Whether a live workspace create targets one of the account's providers (signout must wait for it)."""
    return bool(account_provider_instance_names(user_id, connection_store) & in_flight_provider_names)


def account_panel_state(record_store: WorkspaceRecordStore | None, user_id: str) -> AccountPanelState:
    """A signed-in account's state on this device: LOCKED exactly when the unlock banner would prompt for it."""
    if record_store is not None and record_store.locked_account_user_ids([user_id]) == [user_id]:
        return AccountPanelState.LOCKED
    return AccountPanelState.SIGNED_IN


def is_connection_owner_eligible(connection_store: ConnectionStore | None, user_id: str) -> bool:
    """Whether ``ConnectionStore.add_connection`` would accept the account as an owner here (judged on its own paths)."""
    return (
        connection_store is not None
        and is_account_unlocked(connection_store.paths, user_id)
        and is_master_password_set_for_account(connection_store.paths, user_id)
    )


def sign_out_account_locally(
    user_id: str,
    session_store: MultiAccountSessionStore,
    connection_store: ConnectionStore | None,
    provider_materializer: ProviderSettingsMaterializer | None,
) -> None:
    """Apply an account's signout to this device's state (the plugin session is already gone).

    Drops the identity cache, forgets the account's connection replica (keeping
    only removals that could not be pushed, so they land after a re-signin),
    and materializes the provider set without the account, which removes its
    account instance and every connection instance it owned. The DEK file and
    the workspace-record replica stay, so re-signin needs no new unlock and
    the account's local docker and lima workspaces keep rendering.
    """
    session_store.invalidate_identity_cache()
    if connection_store is not None:
        connection_store.retain_unpushed_removals(user_id)
    if provider_materializer is not None:
        provider_materializer.materialize()


def hand_off_default_account(
    signed_out_user_id: str, session_store: MultiAccountSessionStore, minds_config: MindsConfig | None
) -> None:
    """Make the first account still signed in the default when the default account signs out.

    With none left the default is cleared, so the next sign-in takes it. The
    default is left as it was when the account listing fails (it cannot name
    a successor) or still lists the account (its signout did not go through).
    """
    if minds_config is None or minds_config.get_default_account_id() != signed_out_user_id:
        return
    remaining = session_store.list_accounts()
    if session_store.is_last_identity_read_failed:
        return
    if any(str(account.user_id) == signed_out_user_id for account in remaining):
        return
    minds_config.set_default_account_id(str(remaining[0].user_id) if remaining else None)


def sign_out_account_via_plugin(
    user_id: str,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore,
    connection_store: ConnectionStore | None,
    provider_materializer: ProviderSettingsMaterializer | None,
    minds_config: MindsConfig | None,
) -> None:
    """Sign ``user_id`` out via the mngr_imbue_cloud plugin and clear local state.

    Resolves the email for ``user_id`` against the cached ``auth list``,
    pushes the account's queued connection changes while its session can
    still do so, runs ``mngr imbue_cloud auth signout --account <email>`` (plugin owns
    the SuperTokens session; only this device's session is revoked, and the
    plugin drops the local tokens even when the revoke cannot land, so this
    works offline), then applies the local half through
    :func:`sign_out_account_locally` and hands the default account on.

    No-ops the plugin half gracefully when the user isn't currently visible
    to the plugin -- the cache is still invalidated so a stale entry can't
    survive. Does nothing at all when the account listing failed: that reads
    like a signed-out account, but the account is still signed in, and tearing
    the local half down would discard its replica (and any queued connection
    change) for nothing.
    """
    session = session_store.get_session(user_id)
    if session is None and session_store.is_last_identity_read_failed:
        logger.warning("Could not list the signed-in accounts; leaving user {} signed in as it is", user_id[:8])
        return
    if session is None:
        logger.warning("No mirrored account for user {}; skipping plugin signout", user_id[:8])
    elif cli is None:
        logger.warning("imbue_cloud_cli is not configured; skipping plugin signout for user {}", user_id[:8])
    else:
        if connection_store is not None:
            # Signout deletes the replica, so a removal still queued (connector unreachable when it was made)
            # must reach the server while the session can still push it, or re-signin would bring it back.
            connection_store.push_dirty(user_id, str(session.email))
        try:
            cli.auth_signout(str(session.email))
        except ImbueCloudCliError as exc:
            logger.warning("`mngr imbue_cloud auth signout` failed for {}: {}", session.email, exc)
    sign_out_account_locally(
        user_id,
        session_store=session_store,
        connection_store=connection_store,
        provider_materializer=provider_materializer,
    )
    hand_off_default_account(user_id, session_store, minds_config)


def sign_out_dead_account(
    user_id: str,
    imbue_cloud_cli: ImbueCloudCli,
    session_store: MultiAccountSessionStore,
    connection_store: ConnectionStore,
    provider_materializer: ProviderSettingsMaterializer,
    minds_config: MindsConfig,
    wake_ui_publisher: Callable[[], None],
) -> None:
    """Sign out an account whose session the connector refused to refresh (the sync pass's callback).

    The same signout the interactive route runs. The retirement happens on
    the scheduler thread, which moves nothing the publisher watches, so the
    open windows are woken explicitly or the account launcher would keep the
    retired account's label.
    """
    sign_out_account_via_plugin(
        user_id,
        cli=imbue_cloud_cli,
        session_store=session_store,
        connection_store=connection_store,
        provider_materializer=provider_materializer,
        minds_config=minds_config,
    )
    wake_ui_publisher()
