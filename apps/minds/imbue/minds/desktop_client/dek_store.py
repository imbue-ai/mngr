"""Per-account data-encryption-key (DEK) storage for workspace sync.

Each signed-in account has a random 32-byte DEK that encrypts its workspace
records' secret payloads. Two files per account live under
``<data_dir>/keys/``:

* ``<user_id>.dek`` -- the raw DEK (0600). Its presence means the account is
  "unlocked" on this device; day-to-day operation never needs the master
  password. Created on first use.
* ``<user_id>.bundle.json`` -- the local mirror of the password-wrapped DEK
  bundle (the same JSON pushed to the connector). Present exactly when the
  account's master password is non-empty; its absence IS the "no master
  password" state, and password verification is an unwrap attempt against it.

The master password's only role in the application is wrapping DEKs: setting
or changing it rewraps each account's DEK (nothing else moves), and clearing
it deletes the bundle (locally and server-side, where the caller also scrubs
synced secrets).

The legacy per-install files (``backup_password_hash`` argon2 hash +
``backup_password`` plaintext convenience copy) are converted once by
:func:`convert_legacy_password_files` and renamed aside with a ``.pre-sync``
suffix; no code path reads them afterwards.
"""

import base64
import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError
from argon2.exceptions import VerificationError
from argon2.exceptions import VerifyMismatchError
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.secret_wrapping import KdfParameters
from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.imbue_common.secret_wrapping import derive_kek
from imbue.imbue_common.secret_wrapping import generate_dek
from imbue.imbue_common.secret_wrapping import generate_kdf_parameters
from imbue.imbue_common.secret_wrapping import unwrap_dek
from imbue.imbue_common.secret_wrapping import wrap_dek
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.errors import SyncCryptoError

_KEYS_DIRNAME = "keys"
_LEGACY_PASSWORD_FILENAME = "backup_password"
_LEGACY_PASSWORD_HASH_FILENAME = "backup_password_hash"
_LEGACY_RETIRED_SUFFIX = ".pre-sync"

_PASSWORD_HASHER = PasswordHasher()


def keys_dir(paths: WorkspacePaths) -> Path:
    """Return the directory holding per-account DEK + bundle-mirror files."""
    return paths.data_dir / _KEYS_DIRNAME


def dek_file_path(paths: WorkspacePaths, user_id: str) -> Path:
    return keys_dir(paths) / f"{user_id}.dek"


def bundle_mirror_path(paths: WorkspacePaths, user_id: str) -> Path:
    return keys_dir(paths) / f"{user_id}.bundle.json"


def _write_secret_bytes(path: Path, content: bytes) -> None:
    """Atomically write a 0600 secret file (temp file + rename, never world-readable)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        tmp_path.rename(path)
    except OSError as e:
        raise SyncCryptoError(f"Could not write {path}: {e}") from e


def load_dek(paths: WorkspacePaths, user_id: str) -> bytes | None:
    """Return the account's raw DEK, or None when this device is locked for it."""
    path = dek_file_path(paths, user_id)
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError as e:
        raise SyncCryptoError(f"Could not read the DEK file at {path}: {e}") from e


def ensure_dek(paths: WorkspacePaths, user_id: str) -> bytes:
    """Return the account's DEK, generating and persisting a fresh one if absent."""
    existing = load_dek(paths, user_id)
    if existing is not None:
        return existing
    dek = generate_dek()
    _write_secret_bytes(dek_file_path(paths, user_id), dek)
    logger.debug("Generated a fresh sync DEK for account {}", user_id[:8])
    return dek


def is_account_unlocked(paths: WorkspacePaths, user_id: str) -> bool:
    """Whether this device holds the account's DEK (day-to-day secrets access works)."""
    return dek_file_path(paths, user_id).is_file()


def delete_dek(paths: WorkspacePaths, user_id: str) -> None:
    """Remove the account's local DEK file (used only by tests / explicit lock flows)."""
    try:
        dek_file_path(paths, user_id).unlink(missing_ok=True)
    except OSError as e:
        raise SyncCryptoError(f"Could not delete the DEK file for {user_id}: {e}") from e


# ---------------------------------------------------------------------------
# Bundle (password-wrapped DEK) helpers
# ---------------------------------------------------------------------------


def wrap_dek_to_bundle_json(dek: bytes, password: SecretStr, key_epoch: int) -> dict[str, object]:
    """Wrap the DEK under the master password; returns the wire-shaped bundle JSON."""
    parameters = generate_kdf_parameters()
    kek = derive_kek(password, parameters)
    wrapped = wrap_dek(kek, dek)
    return {
        "kdf_salt": base64.b64encode(parameters.salt).decode("ascii"),
        "kdf_time_cost": parameters.time_cost,
        "kdf_memory_kib": parameters.memory_kib,
        "kdf_parallelism": parameters.parallelism,
        "wrapped_dek": base64.b64encode(wrapped).decode("ascii"),
        "key_epoch": key_epoch,
    }


def unwrap_bundle_json(bundle: Mapping[str, object], password: SecretStr) -> bytes:
    """Recover the DEK from a wire-shaped bundle; raises SecretWrappingError on a wrong password."""
    parameters = KdfParameters(
        salt=base64.b64decode(str(bundle["kdf_salt"])),
        time_cost=int(str(bundle["kdf_time_cost"])),
        memory_kib=int(str(bundle["kdf_memory_kib"])),
        parallelism=int(str(bundle["kdf_parallelism"])),
    )
    kek = derive_kek(password, parameters)
    return unwrap_dek(kek, base64.b64decode(str(bundle["wrapped_dek"])))


def read_bundle_mirror(paths: WorkspacePaths, user_id: str) -> dict[str, object] | None:
    """Return the locally-mirrored bundle JSON, or None when no master password is set."""
    path = bundle_mirror_path(paths, user_id)
    if not path.is_file():
        return None
    try:
        raw = path.read_text()
    except OSError as e:
        raise SyncCryptoError(f"Could not read the bundle mirror at {path}: {e}") from e
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        # The mirror is internal state; a corrupt copy must not brick startup.
        # The server copy (or a fresh password set) rewrites it.
        logger.warning("Ignoring corrupt bundle mirror at {}: {}", path, e)
        return None
    return parsed if isinstance(parsed, dict) else None


def write_bundle_mirror(paths: WorkspacePaths, user_id: str, bundle: Mapping[str, object]) -> None:
    _write_secret_bytes(bundle_mirror_path(paths, user_id), json.dumps(dict(bundle), indent=2).encode("utf-8"))


def delete_bundle_mirror(paths: WorkspacePaths, user_id: str) -> None:
    try:
        bundle_mirror_path(paths, user_id).unlink(missing_ok=True)
    except OSError as e:
        raise SyncCryptoError(f"Could not delete the bundle mirror for {user_id}: {e}") from e


def is_master_password_set_for_account(paths: WorkspacePaths, user_id: str) -> bool:
    """Whether the account has a non-empty master password (a bundle exists exactly then)."""
    return bundle_mirror_path(paths, user_id).is_file()


def verify_master_password_for_account(paths: WorkspacePaths, user_id: str, candidate: SecretStr) -> bool:
    """Check ``candidate`` by attempting the unwrap; no bundle means only the empty password matches."""
    bundle = read_bundle_mirror(paths, user_id)
    if bundle is None:
        return candidate.get_secret_value() == ""
    try:
        unwrap_bundle_json(bundle, candidate)
    except SecretWrappingError:
        return False
    return True


def set_master_password_for_account(
    paths: WorkspacePaths, user_id: str, new_password: SecretStr
) -> dict[str, object] | None:
    """(Re)wrap the account's DEK under ``new_password`` and update the local mirror.

    Returns the new bundle JSON (for the caller to push to the connector), or
    None when the new password is empty -- the mirror is deleted and the caller
    is responsible for the server-side bundle delete + secrets scrub.
    """
    dek = ensure_dek(paths, user_id)
    previous = read_bundle_mirror(paths, user_id)
    key_epoch = int(str(previous["key_epoch"])) if previous is not None and "key_epoch" in previous else 1
    if not new_password.get_secret_value():
        delete_bundle_mirror(paths, user_id)
        return None
    bundle = wrap_dek_to_bundle_json(dek, new_password, key_epoch)
    write_bundle_mirror(paths, user_id, bundle)
    return bundle


def unlock_account_with_bundle(
    paths: WorkspacePaths, user_id: str, bundle: Mapping[str, object], password: SecretStr
) -> bytes:
    """Unwrap a (server-fetched) bundle and persist both the DEK and the mirror.

    Raises ``SecretWrappingError`` when the password is wrong. This is the
    new-device unlock: afterwards the account works without the password.
    """
    dek = unwrap_bundle_json(bundle, password)
    _write_secret_bytes(dek_file_path(paths, user_id), dek)
    write_bundle_mirror(paths, user_id, bundle)
    return dek


# ---------------------------------------------------------------------------
# Legacy backup_password / backup_password_hash conversion
# ---------------------------------------------------------------------------


def _legacy_password_path(paths: WorkspacePaths) -> Path:
    return paths.data_dir / _LEGACY_PASSWORD_FILENAME


def _legacy_hash_path(paths: WorkspacePaths) -> Path:
    return paths.data_dir / _LEGACY_PASSWORD_HASH_FILENAME


def _matches_legacy_hash(stored_hash: str, candidate: str) -> bool:
    if not stored_hash:
        return candidate == ""
    try:
        _PASSWORD_HASHER.verify(stored_hash, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def _read_legacy_master_password(paths: WorkspacePaths) -> SecretStr | None:
    """Recover the legacy master password when it is knowable, else None.

    Knowable means: the plaintext convenience copy exists and matches the
    hash, or the hash is the empty-password seed (password = ""). A non-empty
    hash with no (valid) plaintext copy is unknowable -- the user keeps their
    repos (workspace keys are untouched) but must set a fresh password.
    """
    hash_path = _legacy_hash_path(paths)
    stored_hash = ""
    if hash_path.is_file():
        try:
            stored_hash = hash_path.read_text().strip()
        except OSError as e:
            logger.warning("Could not read the legacy password hash at {}: {}", hash_path, e)
            return None
    plaintext_path = _legacy_password_path(paths)
    if plaintext_path.is_file():
        try:
            saved = plaintext_path.read_text().strip()
        except OSError as e:
            logger.warning("Could not read the legacy password copy at {}: {}", plaintext_path, e)
            saved = ""
        if saved and _matches_legacy_hash(stored_hash, saved):
            return SecretStr(saved)
    if _matches_legacy_hash(stored_hash, ""):
        return SecretStr("")
    return None


def _retire_legacy_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        path.rename(path.with_name(path.name + _LEGACY_RETIRED_SUFFIX))
    except OSError as e:
        logger.warning("Could not retire the legacy password file {}: {}", path, e)


def convert_legacy_password_files(paths: WorkspacePaths, user_ids: Sequence[str]) -> None:
    """One-shot conversion of the legacy per-install password files into per-account bundles.

    For each signed-in account: ensure a DEK exists, and -- when the legacy
    master password is recoverable and non-empty -- wrap the DEK with it so
    the user's password carries over seamlessly. The legacy files are then
    renamed aside (``.pre-sync``); an unrecoverable non-empty password means
    the user starts in the "no master password" state and sets a fresh one
    (their repos stay reachable through the untouched workspace keys).

    Idempotent: a second run finds no legacy files and does nothing. No-op
    (files kept) when no account is signed in yet, so a pre-signin install
    converts on the first signed-in run.
    """
    has_legacy = _legacy_hash_path(paths).is_file() or _legacy_password_path(paths).is_file()
    if not has_legacy or not user_ids:
        return
    legacy_password = _read_legacy_master_password(paths)
    for user_id in user_ids:
        ensure_dek(paths, user_id)
        if (
            legacy_password is not None
            and legacy_password.get_secret_value()
            and not is_master_password_set_for_account(paths, user_id)
        ):
            set_master_password_for_account(paths, user_id, legacy_password)
            logger.info("Carried the legacy backup master password over to account {}", user_id[:8])
    if legacy_password is None:
        logger.warning(
            "The legacy backup master password could not be recovered (hash present, no saved copy); "
            "existing repos stay reachable via their workspace keys, but a fresh master password must be set "
            "on the Settings page before secrets can sync."
        )
    _retire_legacy_file(_legacy_password_path(paths))
    _retire_legacy_file(_legacy_hash_path(paths))
