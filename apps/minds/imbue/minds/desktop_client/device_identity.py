import os
import tempfile
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.errors import DeviceIdError
from imbue.mngr.primitives import HostId

DEVICE_ID_FILENAME: Final[str] = "device_id"
# Installs that predate the minds-owned device id file used the mngr local
# provider's host id (``<mngr_host_dir>/host_id``) as their device identity.
_LEGACY_MNGR_HOST_ID_FILENAME: Final[str] = "host_id"


def get_or_create_device_id(data_dir: Path, mngr_host_dir: Path) -> HostId:
    """Return this install's stable device id, creating ``<data_dir>/device_id`` on first use.

    The device id stamps locally-hosted workspace records (``hosting_device_id``)
    so the sync reconcile can recognize this install's own rows. It is
    ``HostId``-shaped (a user's machine is a host). First creation adopts the
    legacy mngr ``host_id`` value when that file exists (copying it -- the
    original is left in place for mngr's local provider) so previously-synced
    records stay attributed to this install; otherwise a fresh id is minted.
    Creation is atomic (a fully-written temp file is hard-linked into place, an
    exclusive publish), so two processes racing on first launch converge on a
    single id and a visible file always holds complete contents.

    Raises ``DeviceIdError`` when either file is unreadable or holds an invalid
    value, or when the id file cannot be created -- minds must never run
    without a valid identity.
    """
    device_id_path = data_dir / DEVICE_ID_FILENAME
    for _attempt in range(2):
        # lexists (not exists) so a dangling symlink squatting on the path is
        # reported accurately here instead of failing the link step below.
        if os.path.lexists(device_id_path) and not device_id_path.is_file():
            raise DeviceIdError(
                f"The device id path at {device_id_path} exists but is not a regular file. "
                "Remove it, then restart minds."
            )
        existing_device_id = _read_host_id_shaped_file(device_id_path, "device id")
        if existing_device_id is not None:
            return existing_device_id

        # Pick the value to persist: the adopted legacy id when present, else a fresh one.
        adopted_device_id = _read_host_id_shaped_file(
            mngr_host_dir / _LEGACY_MNGR_HOST_ID_FILENAME, "legacy mngr host id"
        )
        new_device_id = adopted_device_id if adopted_device_id is not None else HostId.generate()

        # Persist it atomically; losing the first-creation race means another
        # process just wrote the file, so loop back and read the winner's value.
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise DeviceIdError(f"Could not create the minds data directory at {data_dir}: {e}") from e
        # Write the full contents to a uniquely-named temp file first, then
        # hard-link it into place: the link is an atomic exclusive publish, so
        # a visible device id file always holds a complete value (a plain
        # O_EXCL create would expose an empty file until the write lands).
        try:
            file_descriptor, temp_path_str = tempfile.mkstemp(
                dir=data_dir, prefix=f"{DEVICE_ID_FILENAME}.", suffix=".tmp"
            )
        except OSError as e:
            raise DeviceIdError(f"Could not create a temp file for the device id in {data_dir}: {e}") from e
        temp_path = Path(temp_path_str)
        try:
            try:
                with os.fdopen(file_descriptor, "w") as device_id_file:
                    device_id_file.write(new_device_id)
            except OSError as e:
                raise DeviceIdError(f"Could not write the device id to the temp file at {temp_path}: {e}") from e
            try:
                os.link(temp_path, device_id_path)
            except FileExistsError:
                logger.debug("Lost the device id creation race; re-reading {}", device_id_path)
                continue
            except OSError as e:
                raise DeviceIdError(f"Could not create the device id file at {device_id_path}: {e}") from e
        finally:
            temp_path.unlink(missing_ok=True)
        if adopted_device_id is not None:
            logger.debug("Adopted the legacy mngr host id as this install's device id ({})", new_device_id)
        else:
            logger.debug("Generated a new device id for this install ({})", new_device_id)
        return new_device_id
    raise DeviceIdError(f"Could not read the device id file at {device_id_path} after losing the creation race twice")


def _read_host_id_shaped_file(path: Path, file_description: str) -> HostId | None:
    """Read and validate a ``HostId`` from a file, returning None when the file is absent.

    Raises ``DeviceIdError`` when the file exists but cannot be read or does
    not hold a valid ``HostId``.
    """
    if not path.is_file():
        return None
    try:
        raw_value = path.read_text().strip()
    except OSError as e:
        raise DeviceIdError(f"Could not read the {file_description} file at {path}: {e}") from e
    try:
        return HostId(raw_value)
    except InvalidRandomIdError as e:
        raise DeviceIdError(
            f"The {file_description} file at {path} does not contain a valid host id: {e}. "
            "Fix or delete the file, then restart minds."
        ) from e
