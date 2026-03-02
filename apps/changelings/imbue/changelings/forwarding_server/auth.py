import json
import secrets
from abc import ABC
from abc import abstractmethod
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.changelings.errors import SigningKeyError
from imbue.changelings.primitives import CookieSigningKey
from imbue.changelings.primitives import OneTimeCode
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.primitives import AgentId

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
    agent_id: AgentId = Field(description="The agent this code grants access to")
    status: OneTimeCodeStatus = Field(description="Current status of this code")


class AuthStoreInterface(MutableModel, ABC):
    """Manages one-time codes and cookie signing for authentication."""

    @abstractmethod
    def validate_and_consume_code(
        self,
        agent_id: AgentId,
        code: OneTimeCode,
    ) -> bool:
        """Validate a one-time code and mark it as used if valid."""

    @abstractmethod
    def get_signing_key(self) -> CookieSigningKey:
        """Return the cookie signing key, generating one if it does not exist."""

    @abstractmethod
    def add_one_time_code(
        self,
        agent_id: AgentId,
        code: OneTimeCode,
    ) -> None:
        """Register a new one-time code for an agent."""

    @abstractmethod
    def list_agent_ids_with_valid_codes(self) -> tuple[AgentId, ...]:
        """Return agent IDs that have at least one valid (unused) code."""


class FileAuthStore(AuthStoreInterface):
    """File-based auth store that persists codes in JSON and signing key on disk."""

    data_directory: Path = Field(frozen=True, description="Directory for auth data files")

    def validate_and_consume_code(
        self,
        agent_id: AgentId,
        code: OneTimeCode,
    ) -> bool:
        with log_span("Validating one-time code for {}", agent_id):
            stored_codes = self._load_codes()

            matching_code_idx: int | None = None
            for idx, stored in enumerate(stored_codes):
                if stored.code == code and stored.agent_id == agent_id:
                    matching_code_idx = idx
                    break

            if matching_code_idx is None:
                logger.debug("Rejected unknown code for {}", agent_id)
                return False

            matched = stored_codes[matching_code_idx]
            if matched.status != OneTimeCodeStatus.VALID:
                logger.debug("Rejected already-{} code for {}", matched.status, agent_id)
                return False

            # Mark as used
            updated_codes = list(stored_codes)
            updated_codes[matching_code_idx] = StoredOneTimeCode(
                code=matched.code,
                agent_id=matched.agent_id,
                status=OneTimeCodeStatus.USED,
            )
            self._save_codes(tuple(updated_codes))
            logger.debug("Accepted and consumed code for {}", agent_id)
            return True

    def get_signing_key(self) -> CookieSigningKey:
        key_path = self.data_directory / _SIGNING_KEY_FILENAME
        if key_path.exists():
            try:
                key_value = key_path.read_text().strip()
            except OSError as e:
                raise SigningKeyError(f"Cannot read signing key from {key_path}") from e
            if not key_value:
                raise SigningKeyError(f"Signing key file is empty: {key_path}")
            return CookieSigningKey(key_value)

        # Generate a new key
        with log_span("Generating new signing key"):
            new_key = secrets.token_urlsafe(_SIGNING_KEY_LENGTH)
            try:
                self.data_directory.mkdir(parents=True, exist_ok=True)
                key_path.write_text(new_key)
                key_path.chmod(0o600)
            except OSError as e:
                raise SigningKeyError(f"Cannot write signing key to {key_path}") from e
            return CookieSigningKey(new_key)

    def add_one_time_code(
        self,
        agent_id: AgentId,
        code: OneTimeCode,
    ) -> None:
        with log_span("Adding one-time code for {}", agent_id):
            existing_codes = self._load_codes()
            new_code = StoredOneTimeCode(
                code=code,
                agent_id=agent_id,
                status=OneTimeCodeStatus.VALID,
            )
            self._save_codes(existing_codes + (new_code,))

    def list_agent_ids_with_valid_codes(self) -> tuple[AgentId, ...]:
        stored_codes = self._load_codes()
        ids: set[str] = set()
        for stored in stored_codes:
            if stored.status == OneTimeCodeStatus.VALID:
                ids.add(str(stored.agent_id))
        return tuple(AgentId(i) for i in sorted(ids))

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
