import json
import secrets
import threading
from abc import ABC
from abc import abstractmethod
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import SigningKeyError
from imbue.minds.primitives import CookieSigningKey
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.utils.file_utils import atomic_write

_SIGNING_KEY_LENGTH: Final[int] = 64

_SIGNING_KEY_FILENAME: Final[str] = "signing_key"

_CODES_FILENAME: Final[str] = "one_time_codes.json"


class OneTimeCodeStatus(UpperCaseStrEnum):
    """Status of a one-time authentication code."""

    VALID = auto()
    USED = auto()
    REVOKED = auto()


class StoredOneTimeCode(FrozenModel):
    """A one-time code with its current usage status."""

    code: OneTimeCode = Field(description="The one-time code value")
    status: OneTimeCodeStatus = Field(description="Current status of this code")


class AuthStoreInterface(MutableModel, ABC):
    """Manages one-time codes and cookie signing for global session authentication."""

    @abstractmethod
    def validate_and_consume_code(
        self,
        code: OneTimeCode,
    ) -> bool:
        """Validate a one-time code and mark it as used if valid."""

    @abstractmethod
    def get_signing_key(self) -> CookieSigningKey:
        """Return the cookie signing key, generating one if it does not exist."""

    @abstractmethod
    def add_one_time_code(
        self,
        code: OneTimeCode,
    ) -> None:
        """Register a new one-time code."""


class FileAuthStore(AuthStoreInterface):
    """File-based auth store that persists codes in JSON and signing key on disk."""

    data_directory: Path = Field(frozen=True, description="Directory for auth data files")

    # Serializes first-time signing-key generation. FastAPI dispatches sync
    # route handlers on a threadpool, so on a fresh data directory the desktop
    # client's startup burst (``/authenticate`` plus the ``/`` redirect target,
    # ``/_chrome``, and ``/welcome`` -- each of which calls ``get_signing_key``)
    # can all reach generation concurrently. Without this lock they would mint
    # *different* keys and race to write; the last writer wins and silently
    # invalidates the cookie just signed with an earlier key, so the next
    # request's ``verify_session_cookie`` fails and the user looks logged out.
    _generation_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def validate_and_consume_code(
        self,
        code: OneTimeCode,
    ) -> bool:
        with log_span("Validating one-time code"):
            stored_codes = self._load_codes()

            matching_code_idx: int | None = None
            for idx, stored in enumerate(stored_codes):
                if stored.code == code:
                    matching_code_idx = idx
                    break

            if matching_code_idx is None:
                logger.debug("Rejected unknown code")
                return False

            matched = stored_codes[matching_code_idx]
            if matched.status != OneTimeCodeStatus.VALID:
                logger.debug("Rejected already-{} code", matched.status)
                return False

            # Mark as used
            updated_codes = list(stored_codes)
            updated_codes[matching_code_idx] = StoredOneTimeCode(
                code=matched.code,
                status=OneTimeCodeStatus.USED,
            )
            self._save_codes(tuple(updated_codes))
            logger.debug("Accepted and consumed code")
            return True

    def get_signing_key(self) -> CookieSigningKey:
        key_path = self.data_directory / _SIGNING_KEY_FILENAME

        # Fast path: a key already exists, so no generation (or locking) needed.
        existing = self._read_signing_key(key_path)
        if existing is not None:
            return existing

        # Generate exactly one key even under concurrent first-time access.
        with self._generation_lock:
            # Re-check under the lock: another thread may have generated the key
            # while we were blocked acquiring it.
            existing = self._read_signing_key(key_path)
            if existing is not None:
                return existing

            with log_span("Generating new signing key"):
                new_key = secrets.token_urlsafe(_SIGNING_KEY_LENGTH)
                try:
                    # atomic_write replaces the file in a single step, so a
                    # concurrent reader never observes an empty or partially
                    # written key file.
                    atomic_write(key_path, new_key)
                    key_path.chmod(0o600)
                except OSError as e:
                    raise SigningKeyError(f"Cannot write signing key to {key_path}") from e
                return CookieSigningKey(new_key)

    def _read_signing_key(self, key_path: Path) -> CookieSigningKey | None:
        """Return the persisted signing key, or ``None`` if it does not exist yet.

        Raises :class:`SigningKeyError` if the file exists but cannot be read or
        is empty. Since :func:`atomic_write` never leaves an empty key file, an
        empty file means genuine corruption -- refuse to silently mint a
        replacement, which would invalidate every live session's cookie.
        """
        if not key_path.exists():
            return None
        try:
            key_value = key_path.read_text().strip()
        except OSError as e:
            raise SigningKeyError(f"Cannot read signing key from {key_path}") from e
        if not key_value:
            raise SigningKeyError(f"Signing key file is empty: {key_path}")
        return CookieSigningKey(key_value)

    def add_one_time_code(
        self,
        code: OneTimeCode,
    ) -> None:
        with log_span("Adding one-time code"):
            existing_codes = self._load_codes()
            new_code = StoredOneTimeCode(
                code=code,
                status=OneTimeCodeStatus.VALID,
            )
            self._save_codes(existing_codes + (new_code,))

    def _load_codes(self) -> tuple[StoredOneTimeCode, ...]:
        codes_path = self.data_directory / _CODES_FILENAME
        if not codes_path.exists():
            return ()
        try:
            raw = json.loads(codes_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load codes from {}: {}", codes_path, e)
            return ()
        return tuple(StoredOneTimeCode.model_validate(entry) for entry in raw)

    def _save_codes(self, codes: tuple[StoredOneTimeCode, ...]) -> None:
        codes_path = self.data_directory / _CODES_FILENAME
        self.data_directory.mkdir(parents=True, exist_ok=True)
        serialized = [c.model_dump(mode="json") for c in codes]
        codes_path.write_text(json.dumps(serialized, indent=2))
