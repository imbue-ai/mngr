"""One-way cleanups of state left behind by older minds builds.

Each migration is a named, idempotent function; they run on every reconcile and no-op once the legacy state is gone.
Steady-state desired configuration lives in ``reconcile.py`` -- only historical teardown belongs here.
"""

import shutil

import tomlkit
from loguru import logger
from tomlkit.items import Table

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.provider_blocks import AWS_PROVIDER_NAME_PREFIX


def apply_settings_document_migrations(doc: tomlkit.TOMLDocument) -> bool:
    """Run every in-document migration; returns whether the document changed."""
    providers_section = doc.setdefault("providers", tomlkit.table())
    is_changed = _remove_legacy_ssh_provider_block(providers_section)
    is_changed = _remove_legacy_ambient_aws_region_blocks(providers_section) or is_changed
    return is_changed


def apply_data_dir_migrations(root: MindsRoot) -> None:
    """Run every filesystem-level migration under the env's data dir."""
    _remove_legacy_leased_host_artifacts(root)


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

    The machine-credential AWS path was a prototype; bring-your-own-key ``byok-aws-<slug>`` accounts (which this prefix match deliberately excludes) are the only AWS path in minds now.
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
        root.data_dir / "ssh" / "dynamic_hosts.toml",
        root.data_dir / "ssh" / "keys" / "leased_host",
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
