import json
import secrets
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final
from typing import TypeVar

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import ApiTokenError
from imbue.minds.errors import SigningKeyError
from imbue.minds.primitives import CookieSigningKey
from imbue.minds.primitives import MindsApiToken
from imbue.minds.primitives import OneTimeCode

_SecretT = TypeVar("_SecretT", bound=SecretStr)

_SIGNING_KEY_LENGTH: Final[int] = 64

_API_TOKEN_LENGTH: Final[int] = 48

_SIGNING_KEY_FILENAME: Final[str] = "signing_key"

_API_TOKEN_FILENAME: Final[str] = "api_token"

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
    def get_api_token(self) -> MindsApiToken:
        """Return the minds API bearer token, generating one if it does not exist."""

    @abstractmethod
    def add_one_time_code(
        self,
        code: OneTimeCode,
    ) -> None:
        """Register a new one-time code."""


class FileAuthStore(AuthStoreInterface):
    """File-based auth store that persists codes in JSON and signing key on disk."""

    data_directory: Path = Field(frozen=True, description="Directory for auth data files")

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
        return self._load_or_generate_secret(
            filename=_SIGNING_KEY_FILENAME,
            secret_byte_length=_SIGNING_KEY_LENGTH,
            log_span_label="Generating new signing key",
            read_error_message="Cannot read signing key from {path}",
            empty_error_message="Signing key file is empty: {path}",
            write_error_message="Cannot write signing key to {path}",
            error_class=SigningKeyError,
            wrap=CookieSigningKey,
        )

    def get_api_token(self) -> MindsApiToken:
        return self._load_or_generate_secret(
            filename=_API_TOKEN_FILENAME,
            secret_byte_length=_API_TOKEN_LENGTH,
            log_span_label="Generating new minds API token",
            read_error_message="Cannot read API token from {path}",
            empty_error_message="API token file is empty: {path}",
            write_error_message="Cannot write API token to {path}",
            error_class=ApiTokenError,
            wrap=MindsApiToken,
        )

    def _load_or_generate_secret(
        self,
        *,
        filename: str,
        secret_byte_length: int,
        log_span_label: str,
        read_error_message: str,
        empty_error_message: str,
        write_error_message: str,
        error_class: type[Exception],
        wrap: Callable[[str], _SecretT],
    ) -> _SecretT:
        """Read a 0o600-permissioned secret file, generating it on first access.

        Shared between :meth:`get_signing_key` and :meth:`get_api_token`.
        The error-message templates each accept a ``{path}`` placeholder
        so each caller can supply its own wording without the helper
        having to know about the specific secret kind.
        """
        secret_path = self.data_directory / filename
        if secret_path.exists():
            try:
                secret_value = secret_path.read_text().strip()
            except OSError as e:
                raise error_class(read_error_message.format(path=secret_path)) from e
            if not secret_value:
                raise error_class(empty_error_message.format(path=secret_path))
            return wrap(secret_value)

        with log_span(log_span_label):
            new_secret = secrets.token_urlsafe(secret_byte_length)
            try:
                self.data_directory.mkdir(parents=True, exist_ok=True)
                secret_path.write_text(new_secret)
                secret_path.chmod(0o600)
            except OSError as e:
                raise error_class(write_error_message.format(path=secret_path)) from e
            return wrap(new_secret)

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
