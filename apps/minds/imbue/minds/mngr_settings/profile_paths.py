import tomllib
from pathlib import Path

from loguru import logger

from imbue.minds.bootstrap import MindsRoot


def read_active_profile_dir(mngr_host_dir: Path) -> Path | None:
    """Return ``<mngr_host_dir>/profiles/<active-profile>``, or None if unresolved.

    The active profile id lives in ``<mngr_host_dir>/config.toml`` under the ``profile`` key.
    Returns None when mngr hasn't been initialized in this host_dir yet (no ``config.toml`` / no ``profile`` key) or when the config can't be read.
    Resolution is inlined here (rather than imported from mngr) because this package must be importable before mngr is.
    """
    config_path = mngr_host_dir / "config.toml"
    if not config_path.is_file():
        return None
    try:
        config_data = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read mngr config {}: {}", config_path, e)
        return None
    profile_id = config_data.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return None
    return mngr_host_dir / "profiles" / profile_id


def resolve_active_settings_path(root: MindsRoot) -> Path | None:
    """Locate the active mngr settings.toml under the env's host_dir.

    Returns ``None`` if mngr hasn't been initialized in this host_dir yet.
    Callers should treat ``None`` as "skip silently" since there's nothing useful to write to.
    """
    settings_dir = read_active_profile_dir(root.mngr_host_dir)
    if settings_dir is None or not settings_dir.exists():
        return None
    return settings_dir / "settings.toml"


def imbue_cloud_accounts_path(root: MindsRoot) -> Path | None:
    """Return the path to the imbue_cloud plugin's ``accounts.json``, or None if no profile is set.

    Mirrors ``mngr_imbue_cloud.config.get_sessions_dir`` / ``get_active_profile_dir``: the accounts index lives at ``<host_dir>/profiles/<profile>/providers/imbue_cloud/sessions/accounts.json``.
    Inlined here because this package must be importable before mngr (which ``imbue.mngr_imbue_cloud`` transitively pulls in).
    """
    profile_dir = read_active_profile_dir(root.mngr_host_dir)
    if profile_dir is None:
        return None
    return profile_dir / "providers" / "imbue_cloud" / "sessions" / "accounts.json"
