import tomlkit
from loguru import logger

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.file_store import settings_store_for


def list_disabled_provider_names(*, root: MindsRoot) -> list[str]:
    """Return provider names that minds' active settings file marks ``is_enabled = false``.

    Used by the providers panel to enumerate the disabled set (which discovery skips and so are absent from the per-provider discovery snapshots).
    Reads only minds' active settings file -- providers disabled solely in mngr's own settings.toml are not surfaced here.
    Returns an empty list when the file does not exist yet (fresh install).
    """
    store = settings_store_for(root)
    if store is None:
        return []
    providers = store.read().get("providers")
    if not isinstance(providers, dict):
        return []
    disabled: list[str] = []
    for name, block in providers.items():
        if isinstance(block, dict) and block.get("is_enabled") is False:
            disabled.append(name)
    return sorted(disabled)


def set_provider_is_enabled(provider_name: str, is_enabled: bool, *, root: MindsRoot) -> bool:
    """Set ``is_enabled`` for the named provider in minds' active settings file.

    Generic over any provider name -- used by minds' providers panel toggle to let the user disable an errored provider (silencing its noise) or re-enable a previously-disabled one.
    If ``[providers.<provider_name>]`` does not exist, creates it with just ``is_enabled`` as an override on top of mngr's merged config.

    Idempotent: returns ``True`` only when the file was actually modified.
    Returns ``False`` (and does nothing) when the minds root is not yet set up.
    """
    store = settings_store_for(root)
    if store is None:
        return False
    is_modified = store.update(
        lambda doc: _set_is_enabled_in_document(doc, provider_name=provider_name, is_enabled=is_enabled)
    )
    if is_modified:
        logger.debug("Set provider {} is_enabled={} in {}", provider_name, is_enabled, store.settings_path)
    return is_modified


def _set_is_enabled_in_document(doc: tomlkit.TOMLDocument, *, provider_name: str, is_enabled: bool) -> bool:
    providers = doc.get("providers")
    if not isinstance(providers, dict):
        providers = tomlkit.table()
        doc["providers"] = providers
    existing = providers.get(provider_name)
    if not isinstance(existing, dict):
        # Block doesn't exist yet -- create it with just is_enabled.
        new_block = tomlkit.table()
        new_block["is_enabled"] = is_enabled
        providers[provider_name] = new_block
        return True
    if existing.get("is_enabled") == is_enabled:
        return False
    existing["is_enabled"] = is_enabled
    return True
