"""Loads the settings carriers from where the values live: ``config.toml`` and the per-account settings records.

Install-keyed values come through their ``MindsConfig`` getters; account-keyed
values come from the named account's replica of the synced ``settings``
document (phase 3A of ``specs/user-profile-state/spec.md``), with ``local``
the device's pseudo-account. The readers only see the carriers, so where the
values live can move again without touching them.
"""

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.settings.resolve import SettingsResolutionContext
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import InstallSettings


def load_settings_context(
    minds_config: MindsConfig | None,
    settings_store: AccountSettingsStore | None,
    # The account the read resolves for: a window's, or a workspace's association ("local" for the device).
    account_id: str,
) -> SettingsResolutionContext:
    """A fresh load of both records for one account; the declared defaults where nothing is wired or stored.

    Each value comes through its own locked read, not one snapshot across the
    stores: the declared settings are independent last-write-wins preferences,
    so a write landing between two reads costs nothing.
    """
    if minds_config is None:
        install = InstallSettings()
    else:
        notification_style, _has_chosen = minds_config.get_notification_style_with_choice()
        install = InstallSettings(
            sign_in_browser=minds_config.get_sign_in_browser_id(),
            report_unexpected_errors=minds_config.get_report_unexpected_errors(),
            notification_style=notification_style,
        )
    account = settings_store.read_settings(account_id) if settings_store is not None else AccountSettings()
    return SettingsResolutionContext(install=install, account=account)
