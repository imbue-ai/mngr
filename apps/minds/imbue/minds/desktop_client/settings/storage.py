"""Loads the settings carriers from where the values live: ``config.toml``, the per-account settings records, and the per-workspace install records.

Install-keyed values come through their ``MindsConfig`` getters; account-keyed
values come from the named account's replica of the synced ``settings``
document (phase 3A of ``specs/user-profile-state/spec.md``), with ``local``
the device's pseudo-account; the values this install holds for one workspace
come from that workspace's record in the install's store
(``specs/workspace-microphone-permission/spec.md``, section 5.2). The readers
only see the carriers, so where the values live can move again without
touching them.
"""

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.settings.resolve import SettingsResolutionContext
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import CLIENT_PERMISSIONS
from imbue.minds.desktop_client.settings.schema import InstallSettings
from imbue.minds.desktop_client.workspace_install_settings_store import WorkspaceInstallSettingsStore
from imbue.mngr.primitives import AgentId


def load_settings_context(
    minds_config: MindsConfig | None,
    settings_store: AccountSettingsStore | None,
    # The account the read resolves for: a window's, or a workspace's association ("local" for the device).
    account_id: str,
    # The workspace the read is inside, when it is inside one; with the store, the values this
    # install holds for that workspace reach the context.
    workspace_id: AgentId | None = None,
    workspace_install_settings_store: WorkspaceInstallSettingsStore | None = None,
) -> SettingsResolutionContext:
    """A fresh load of the records for one account (and one workspace); the declared defaults where nothing is wired or stored.

    Each value comes through its own locked read, not one snapshot across the
    stores: the declared settings are independent last-write-wins preferences,
    so a write landing between two reads costs nothing.
    """
    if minds_config is None:
        install = InstallSettings()
    else:
        install = InstallSettings(
            sign_in_browser=minds_config.get_sign_in_browser_id(),
            report_unexpected_errors=minds_config.get_report_unexpected_errors(),
            notification_style=minds_config.get_notification_style(),
            allow_agent_command_execution=minds_config.get_allow_agent_command_execution(),
        )
    account = settings_store.read_settings(account_id) if settings_store is not None else AccountSettings()
    install_workspace_override_by_key: dict[str, object] = {}
    if workspace_id is not None and workspace_install_settings_store is not None:
        workspace_record = workspace_install_settings_store.read(str(workspace_id))
        if workspace_record.client_permissions:
            install_workspace_override_by_key[CLIENT_PERMISSIONS.key] = workspace_record.client_permissions
    return SettingsResolutionContext(
        install=install, account=account, install_workspace_override_by_key=install_workspace_override_by_key
    )
