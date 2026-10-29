"""One-time move of the account-keyed settings out of ``config.toml`` (spec section 7.5, phase 3A).

On the first start of a phase-3A build, the values ``config.toml`` still
holds become the device record and a seed for every signed-in account, and
the moved keys are deleted. Seeds are stored clean at revision 0: the first
pull replaces one when the server already holds a settings document (the
existing one wins), and otherwise promotes it to the account's initial
document. A config the migration has already visited holds none of the moved
keys, so the whole thing is a no-op from the second start on.
"""

# CLEANUP: remove this module, its MindsConfig helpers, and its call in
# cli/run.py once every supported install has started a phase-3A build.

from collections.abc import Sequence

from loguru import logger

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.minds_config import MindsConfig


def migrate_account_keyed_settings(
    minds_config: MindsConfig,
    settings_store: AccountSettingsStore,
    account_user_ids: Sequence[str] | None,
) -> bool:
    """Seed the settings records from ``config.toml``'s moved keys, then delete them; returns whether anything moved.

    ``account_user_ids`` is ``None`` when the signed-in accounts could not be
    listed: the move is one-shot and deleting the keys without every account's
    seed would lose those accounts' settings for good, so the whole migration
    waits for a start that can list them.
    """
    if account_user_ids is None:
        logger.warning("Deferring the account-keyed settings migration: the signed-in accounts could not be listed")
        return False
    values = minds_config.read_account_keyed_settings_for_migration()
    if not values:
        return False
    for user_id in (LOCAL_PROFILE_USER_ID, *account_user_ids):
        settings_store.seed_if_absent(user_id, values)
    minds_config.delete_account_keyed_settings_for_migration()
    logger.info(
        "Moved {} account-keyed setting(s) out of config.toml into the settings records of {} account(s)",
        len(values),
        1 + len(account_user_ids),
    )
    return True
