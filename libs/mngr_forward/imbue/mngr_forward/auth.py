"""In-memory one-time-code store + on-disk signing key.

Adapted from ``minds.desktop_client.auth``. The disk persistence of one-time
codes (``one_time_codes.json``) is intentionally dropped — codes only need to
survive within a single plugin run, and persisting them across restarts let
codes from a previous session re-authenticate stale tabs. The signing key is
still persisted so cookies issued before a restart continue to verify.
"""

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
from imbue.mngr_forward.errors import ForwardAuthError
from imbue.mngr_forward.primitives import CookieSigningKey
from imbue.mngr_forward.primitives import OneTimeCode

_SIGNING_KEY_LENGTH: Final[int] = 64

_SIGNING_KEY_FILENAME: Final[str] = "signing_key"


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
    """File-backed signing key + in-memory one-time codes.

    The signing key is read from ``<data_directory>/signing_key`` and
    generated once if missing. Once issued, it is reused on every subsequent
    process start so cookies issued by previous runs continue to verify.

    One-time codes live only in memory: a fresh process always starts with no
    codes, and codes that were issued but never consumed do not leak across
    restarts.
    """

    data_directory: Path = Field(frozen=True, description="Directory for the signing key file")

    _codes_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _codes: dict[OneTimeCode, OneTimeCodeStatus] = PrivateAttr(default_factory=dict)

    def validate_and_consume_code(
        self,
        code: OneTimeCode,
    ) -> bool:
        with log_span("Validating one-time code"):
            with self._codes_lock:
                status = self._codes.get(code)
                if status is None:
                    logger.debug("Rejected unknown code")
                    return False
                if status != OneTimeCodeStatus.VALID:
                    logger.debug("Rejected already-{} code", status)
                    return False
                self._codes[code] = OneTimeCodeStatus.USED
                logger.debug("Accepted and consumed code")
                return True

    def get_signing_key(self) -> CookieSigningKey:
        key_path = self.data_directory / _SIGNING_KEY_FILENAME
        if key_path.exists():
            try:
                key_value = key_path.read_text().strip()
            except OSError as e:
                raise ForwardAuthError(f"Cannot read signing key from {key_path}") from e
            if not key_value:
                raise ForwardAuthError(f"Signing key file is empty: {key_path}")
            return CookieSigningKey(key_value)

        with log_span("Generating new signing key"):
            new_key = secrets.token_urlsafe(_SIGNING_KEY_LENGTH)
            try:
                self.data_directory.mkdir(parents=True, exist_ok=True)
                key_path.write_text(new_key)
                key_path.chmod(0o600)
            except OSError as e:
                raise ForwardAuthError(f"Cannot write signing key to {key_path}") from e
            return CookieSigningKey(new_key)

    def add_one_time_code(
        self,
        code: OneTimeCode,
    ) -> None:
        with log_span("Adding one-time code"):
            with self._codes_lock:
                self._codes[code] = OneTimeCodeStatus.VALID
