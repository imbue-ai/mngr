import os
import stat
import tempfile
import tomllib
from pathlib import Path
from uuid import uuid4

from loguru import logger

from imbue.minds.bootstrap import MindsRoot


def _read_root_config(mngr_host_dir: Path) -> dict[str, object] | None:
    """The parsed ``<mngr_host_dir>/config.toml``: empty when the file is absent, None (after a warning) when it can't be read."""
    config_path = mngr_host_dir / "config.toml"
    if not config_path.is_file():
        return {}
    try:
        return tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read mngr config {}: {}", config_path, e)
        return None


def _profile_id_from(root_config: dict[str, object]) -> str | None:
    profile_id = root_config.get("profile")
    if not isinstance(profile_id, str) or not profile_id:
        return None
    return profile_id


def read_active_profile_dir(mngr_host_dir: Path) -> Path | None:
    """Return ``<mngr_host_dir>/profiles/<active-profile>``, or None if unresolved.

    The active profile id lives in ``<mngr_host_dir>/config.toml`` under the ``profile`` key.
    Returns None when there is no ``config.toml`` / no ``profile`` key, or when the config can't be read.
    Resolution is inlined here (rather than imported from mngr) because this package must be importable before mngr is.
    """
    root_config = _read_root_config(mngr_host_dir)
    profile_id = None if root_config is None else _profile_id_from(root_config)
    if profile_id is None:
        return None
    return mngr_host_dir / "profiles" / profile_id


def ensure_active_profile_dir(mngr_host_dir: Path) -> None:
    """Create ``<mngr_host_dir>/profiles/<active-profile>`` if it is missing.

    Reuses the id ``config.toml`` names; otherwise generates one and replaces ``config.toml`` with one naming it.
    A ``config.toml`` that can't be read is left untouched for mngr to report.
    """
    root_config = _read_root_config(mngr_host_dir)
    if root_config is None:
        return
    existing_profile_id = _profile_id_from(root_config)
    profile_id = existing_profile_id or uuid4().hex
    (mngr_host_dir / "profiles" / profile_id).mkdir(parents=True, exist_ok=True)
    if existing_profile_id is None:
        _write_text_atomically(mngr_host_dir / "config.toml", f'profile = "{profile_id}"\n')


def _write_text_atomically(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, writing through a symlink and keeping an existing file's mode."""
    target = path.resolve()
    existing_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    with tempfile.NamedTemporaryFile("w", dir=target.parent, suffix=".tmp", delete=False) as tmp_file:
        tmp_file.write(text)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    tmp_path = Path(tmp_file.name)
    try:
        if existing_mode is not None:
            tmp_path.chmod(existing_mode)
        os.replace(tmp_path, target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def resolve_active_settings_path(root: MindsRoot) -> Path | None:
    """Locate the active mngr settings.toml under the env's host_dir.

    Returns ``None`` when the host_dir has no active profile dir.
    Callers should treat ``None`` as "skip silently" since there's nothing useful to write to.
    """
    return resolve_active_settings_path_in(root.mngr_host_dir)


def resolve_active_settings_path_in(mngr_host_dir: Path) -> Path | None:
    """:func:`resolve_active_settings_path` for a caller that holds the host dir rather than the env's root."""
    settings_dir = read_active_profile_dir(mngr_host_dir)
    if settings_dir is None or not settings_dir.exists():
        return None
    return settings_dir / "settings.toml"
