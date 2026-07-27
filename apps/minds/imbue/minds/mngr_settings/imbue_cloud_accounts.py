import json

import tomlkit
from loguru import logger

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.errors import MindsSettingsError
from imbue.minds.mngr_settings.file_store import settings_store_for
from imbue.minds.mngr_settings.profile_paths import imbue_cloud_accounts_path
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_BACKEND_NAME
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_DEFAULT_START_ARGS
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_DOCKER_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_LOG_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_VOLUME_HOME_PATH
from imbue.minds.mngr_settings.provider_blocks import imbue_cloud_provider_name_for_account
from imbue.minds.mngr_settings.provider_blocks import remove_provider_block
from imbue.minds.mngr_settings.reconcile import ensure_mngr_settings


def set_imbue_cloud_provider_for_account(
    email: str,
    *,
    connector_url: str,
    root: MindsRoot,
    force_enable: bool = True,
) -> bool:
    """Register ``[providers.imbue_cloud_<slug>]`` in mngr's settings.toml.

    Called by minds when a SuperTokens session for ``email`` is created (signin/signup/oauth-success) and from the bootstrap reconcile.
    Idempotent: a no-op if an equivalent entry already exists.

    ``connector_url`` is the URL of the ``remote_service_connector`` the provider should talk to; callers source it from the loaded ``ClientEnvConfig``.

    When ``force_enable`` is True (signin events), ``is_enabled`` is set to True even if the block was previously disabled via the providers panel.
    When False (bootstrap reconcile on a returning user), any pre-existing ``is_enabled`` value is preserved so a disabled account stays disabled until the user signs in again.

    Returns ``True`` when the file was modified, so callers know whether to bounce ``mngr observe`` (the running process needs a restart to see the new provider instance).

    Always (re-)runs :func:`ensure_mngr_settings` first: at minds startup the mngr profile dir may not have existed yet (making the startup ensure a no-op), but by the time a signin fires, mngr has been initialized and the ensure lands the overrides the startup call missed.
    """
    # Fold the minds-side-overrides write into the returned "modified" flag: if this call (rather than the startup bootstrap) is what first landed the suppression blocks, the observe process needs a bounce for them too.
    is_settings_modified = ensure_mngr_settings(root)
    store = settings_store_for(root)
    if store is None:
        return is_settings_modified
    provider_name = imbue_cloud_provider_name_for_account(email)

    is_account_modified = store.update(
        lambda doc: _register_account_block(doc, email=email, connector_url=connector_url, force_enable=force_enable)
    )
    if is_account_modified:
        logger.debug("imbue_cloud provider {} registered in {}", provider_name, store.settings_path)
    return is_settings_modified or is_account_modified


def unset_imbue_cloud_provider_for_account(email: str, *, root: MindsRoot) -> bool:
    """Remove ``[providers.imbue_cloud_<slug>]`` from mngr's settings.toml.

    Called by minds on signout.
    Idempotent: a no-op if no such entry exists.
    Returns ``True`` when the file was modified.
    """
    store = settings_store_for(root)
    if store is None:
        return False
    provider_name = imbue_cloud_provider_name_for_account(email)
    is_modified = store.update(lambda doc: remove_provider_block(doc, provider_name))
    if is_modified:
        logger.debug("imbue_cloud provider {} removed from {}", provider_name, store.settings_path)
    return is_modified


def is_imbue_cloud_provider_enabled_for_account(email: str, *, root: MindsRoot) -> bool:
    """Return whether ``[providers.imbue_cloud_<slug>]`` is currently enabled.

    Reads the ``is_enabled`` field from the active mngr settings.toml so the desktop UI can render "Signed out" on a chip whose provider the user disabled via the providers panel.
    Treats a missing entry or a missing ``is_enabled`` field as enabled (per mngr's default), and returns True when the settings file can't be located so the UI never erroneously claims an account is signed out before the bootstrap has finished writing the block.
    """
    store = settings_store_for(root)
    if store is None:
        return True
    try:
        provider_name = imbue_cloud_provider_name_for_account(email)
    except MindsSettingsError:
        return True
    providers = store.read().get("providers")
    if not isinstance(providers, dict):
        return True
    block = providers.get(provider_name)
    if not isinstance(block, dict):
        return True
    is_enabled = block.get("is_enabled", True)
    return bool(is_enabled)


def reconcile_imbue_cloud_providers_from_sessions(connector_url: str, *, root: MindsRoot) -> bool:
    """Re-register ``[providers.imbue_cloud_<slug>]`` for every active session.

    Returns ``True`` when any settings write happened, so a caller not already restarting the observe process can bounce it.

    The mngr_imbue_cloud plugin owns the SuperTokens session list (``accounts.json``), which mngr updates on every signin/signup/oauth and on signout.
    The provider-instance registration in settings.toml is only written by the signin *event*, which does not fire on cookie-resumed startups, so the on-disk state can drift to "signed in per the plugin, but no provider block" -- at which point ``mngr create`` fails with ``Unknown provider backend``.
    Walking the accounts index on every minds startup and ensuring each email has a registered provider entry costs essentially nothing (the set call is a no-op when the entry already matches) and makes the bootstrap idempotent over arbitrary settings.toml drift.

    No-op when the accounts file doesn't exist yet (fresh install with no signins).
    """
    # Re-run the ensure here too: the startup call no-ops on a freshly-created env whose mngr profile dir doesn't exist yet, so returning users who never re-signin still need the overrides landed on their next startup.
    is_modified = ensure_mngr_settings(root)
    accounts_path = imbue_cloud_accounts_path(root)
    if accounts_path is None or not accounts_path.is_file():
        return is_modified
    try:
        raw = accounts_path.read_text()
    except OSError as e:
        logger.warning("Could not read imbue_cloud accounts index {}: {}", accounts_path, e)
        return is_modified
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Malformed imbue_cloud accounts index {}: {}", accounts_path, e)
        return is_modified
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return is_modified
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if not isinstance(email, str) or not email:
            continue
        try:
            # Reconcile only fills in missing blocks; it must not re-enable a provider that the user previously disabled via the providers panel.
            # Re-enable happens only on an explicit signin event.
            is_account_modified = set_imbue_cloud_provider_for_account(
                email,
                connector_url=connector_url,
                root=root,
                force_enable=False,
            )
            is_modified = is_modified or is_account_modified
        except MindsSettingsError as e:
            # Bad email format (e.g. ``""``) -- log and keep going so a single corrupt session entry doesn't block reconciliation for the others.
            logger.warning("Skipping imbue_cloud provider registration for {!r}: {}", email, e)
    return is_modified


def _register_account_block(doc: tomlkit.TOMLDocument, *, email: str, connector_url: str, force_enable: bool) -> bool:
    provider_name = imbue_cloud_provider_name_for_account(email)
    providers = doc.setdefault("providers", tomlkit.table())
    existing = providers.get(provider_name)
    existing_is_enabled = existing.get("is_enabled") if isinstance(existing, dict) else None
    desired_is_enabled = True if force_enable else existing_is_enabled
    if (
        isinstance(existing, dict)
        and existing.get("backend") == IMBUE_CLOUD_BACKEND_NAME
        and existing.get("account") == email
        and existing.get("connector_url") == connector_url
        and existing_is_enabled == desired_is_enabled
        and existing.get("docker_runtime") == IMBUE_CLOUD_DOCKER_RUNTIME
        and existing.get("install_gvisor_runtime") == IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME
        and existing.get("default_start_args") == list(IMBUE_CLOUD_DEFAULT_START_ARGS)
        and existing.get("host_dir") == WORKSPACE_HOST_DIR
        and existing.get("volume_home_path") == WORKSPACE_VOLUME_HOME_PATH
        and existing.get("host_log_dir") == WORKSPACE_HOST_LOG_DIR
    ):
        return False
    new_block = tomlkit.table()
    new_block["backend"] = IMBUE_CLOUD_BACKEND_NAME
    new_block["account"] = email
    new_block["connector_url"] = connector_url
    if desired_is_enabled is not None:
        new_block["is_enabled"] = desired_is_enabled
    # Run the rebuilt agent container under gVisor with the runsc hardening args (see provider_blocks).
    new_block["docker_runtime"] = IMBUE_CLOUD_DOCKER_RUNTIME
    new_block["install_gvisor_runtime"] = IMBUE_CLOUD_INSTALL_GVISOR_RUNTIME
    new_block["default_start_args"] = list(IMBUE_CLOUD_DEFAULT_START_ARGS)
    # The user-data layout knobs (see provider_blocks).
    new_block["host_dir"] = WORKSPACE_HOST_DIR
    new_block["volume_home_path"] = WORKSPACE_VOLUME_HOME_PATH
    new_block["host_log_dir"] = WORKSPACE_HOST_LOG_DIR
    providers[provider_name] = new_block
    return True
