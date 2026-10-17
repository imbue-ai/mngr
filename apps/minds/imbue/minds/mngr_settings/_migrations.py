"""One-way cleanups of state left behind by older Imbue Studio builds.

Each migration is a named, idempotent function; they run on every reconcile and no-op once the legacy state is gone.
Steady-state desired configuration lives in ``reconcile.py`` -- only historical teardown belongs here.
"""

import shutil
from typing import Final

import tomlkit
from loguru import logger
from tomlkit.items import Table

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.interfaces import MindsSettingsStoreInterface
from imbue.minds.mngr_settings.provider_blocks import AWS_PROVIDER_NAME_PREFIX
from imbue.minds.mngr_settings.provider_blocks import MODAL_BACKEND_NAME
from imbue.minds.mngr_settings.provider_blocks import MODAL_MODE_DIRECT


def apply_settings_document_migrations(doc: tomlkit.TOMLDocument) -> bool:
    """Run every in-document migration; returns whether the document changed."""
    providers_section = doc.setdefault("providers", tomlkit.table())
    is_changed = _remove_legacy_ssh_provider_block(providers_section)
    is_changed = _remove_legacy_ambient_aws_region_blocks(providers_section) or is_changed
    is_changed = _rewrite_legacy_ambient_modal_block(providers_section) or is_changed
    return is_changed


def apply_data_dir_migrations(root: MindsRoot) -> None:
    """Run every filesystem-level migration under the env's data dir."""
    _remove_legacy_leased_host_artifacts(root)


# CLEANUP: drop apply_one_shot_settings_migrations, _clear_panel_provider_disables
# and the marker file once every install has started a release carrying it (a
# release or two after the first one without the providers-panel toggle).
_PANEL_PROVIDER_DISABLES_CLEARED_MARKER: Final[str] = "panel_provider_disables_cleared"
_PANEL_TOGGLE_PROVIDER_NAMES: Final[tuple[str, ...]] = ("docker", "lima", "vultr")


def apply_one_shot_settings_migrations(root: MindsRoot, store: MindsSettingsStoreInterface) -> bool:
    """Run the migrations that must touch the settings exactly once, each recorded by a marker file; returns whether the file was written.

    The retired providers-panel toggle could write ``is_enabled = false`` into
    the docker, lima and vultr blocks of the profile, and without the toggle
    such a provider stays hidden with no way back from the UI. The marker is
    written only after a successful save, so a failed save retries on the next
    reconcile, while a later deliberate hand edit is left alone.
    """
    marker_path = root.state_dir / "migrations" / _PANEL_PROVIDER_DISABLES_CLEARED_MARKER
    if marker_path.exists():
        return False
    is_changed = store.update(_clear_panel_provider_disables)
    if is_changed:
        logger.info("Cleared the disabled flag the retired providers-panel toggle wrote into the profile's settings")
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.touch()
    return is_changed


def _clear_panel_provider_disables(doc: tomlkit.TOMLDocument) -> bool:
    providers_section = doc.get("providers")
    if not isinstance(providers_section, dict):
        return False
    is_changed = False
    for name in _PANEL_TOGGLE_PROVIDER_NAMES:
        block = providers_section.get(name)
        if not isinstance(block, dict) or block.get("is_enabled") is not False:
            continue
        del block["is_enabled"]
        if not block:
            del providers_section[name]
        is_changed = True
    return is_changed


def _remove_legacy_ssh_provider_block(providers_section: Table | tomlkit.TOMLDocument) -> bool:
    """Old builds wrote a ``[providers.ssh]`` block for the leased-host SSH dance; remove it.

    The imbue_cloud provider owns that path now (it talks to the connector service directly), and the stale block made ``mngr list`` discovery fan out to long-destroyed hosts.
    """
    if "ssh" not in providers_section:
        return False
    del providers_section["ssh"]
    return True


def _remove_legacy_ambient_aws_region_blocks(providers_section: Table | tomlkit.TOMLDocument) -> bool:
    """Old builds wrote one ambient ``[providers.aws-<region>]`` block per configured region; remove them.

    The machine-credential AWS path was a prototype; the ``aws_<hex>`` connection instances the materializer writes are the AWS path in minds now, and the ``aws-`` prefix match deliberately leaves every other block alone.
    mngr CLI users' own settings are unaffected -- this is minds' profile settings file.
    """
    legacy_names = [name for name in providers_section if name.startswith(AWS_PROVIDER_NAME_PREFIX)]
    for name in legacy_names:
        del providers_section[name]
    return bool(legacy_names)


def _remove_legacy_leased_host_artifacts(root: MindsRoot) -> None:
    """Remove the stale ``ssh/dynamic_hosts.toml`` file and ``ssh/keys/leased_host/`` dir.

    Both belonged to the leased-host SSH-provider mechanism the imbue_cloud provider replaced; ``dynamic_hosts.toml`` in particular holds entries pointing at long-destroyed VPS IPs that block any reader on TCP timeouts.
    Best-effort: log + continue on any FS error.
    """
    legacy_paths = (
        root.state_dir / "ssh" / "dynamic_hosts.toml",
        root.state_dir / "ssh" / "keys" / "leased_host",
    )
    for path in legacy_paths:
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:
            logger.warning("Could not remove legacy minds-leased-host artifact {}: {}", path, e)
        else:
            logger.debug("Removed legacy minds-leased-host artifact {}", path)


# CLEANUP: drop _rewrite_legacy_ambient_modal_block (and its call in
# apply_settings_document_migrations) once no profile written by a build that
# used the ambient DIRECT ``[providers.modal]`` block (before Modal became a
# connection) is expected to exist.
def _rewrite_legacy_ambient_modal_block(providers_section: Table | tomlkit.TOMLDocument) -> bool:
    """Old builds wrote an ambient ``[providers.modal]`` DIRECT block (the local Modal token); collapse it to the suppression form.

    Modal is a connection with its own credentials now (``[providers.modal_<hex>]``, written by the materializer), so the default instance is suppressed like aws/gcp/azure.
    Recognized by ``mode = "DIRECT"``: the block is replaced whole, dropping the DIRECT-mode pins and any panel-written ``is_enabled``.
    """
    block = providers_section.get(MODAL_BACKEND_NAME)
    if not isinstance(block, dict) or block.get("mode") != MODAL_MODE_DIRECT:
        return False
    suppression = tomlkit.table()
    suppression["backend"] = MODAL_BACKEND_NAME
    suppression["is_enabled"] = False
    providers_section[MODAL_BACKEND_NAME] = suppression
    return True
