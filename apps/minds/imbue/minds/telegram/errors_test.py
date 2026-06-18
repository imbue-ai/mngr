"""Tests for the telegram error hierarchy.

These assert the *catch* contracts callers rely on (e.g. that a credential
error can be handled as a plain ValueError, or that any telegram error can be
handled via the shared TelegramError / MindError base) by actually raising and
catching, rather than only asserting issubclass relationships.
"""

import pytest

from imbue.minds.errors import MindError
from imbue.minds.errors import TelegramBotCreationError
from imbue.minds.errors import TelegramCredentialError
from imbue.minds.errors import TelegramCredentialExtractionError
from imbue.minds.errors import TelegramError


def test_telegram_error_is_catchable_as_mind_error() -> None:
    with pytest.raises(MindError):
        raise TelegramError("boom")


def test_telegram_credential_error_is_catchable_as_value_error() -> None:
    # Callers that validate inputs catch ValueError; the credential error must
    # be handled by such a handler.
    with pytest.raises(ValueError):
        raise TelegramCredentialError("invalid credentials")


def test_telegram_credential_error_is_catchable_as_telegram_error() -> None:
    with pytest.raises(TelegramError):
        raise TelegramCredentialError("invalid credentials")


def test_telegram_extraction_error_is_catchable_as_telegram_error() -> None:
    with pytest.raises(TelegramError):
        raise TelegramCredentialExtractionError("extraction failed")


def test_telegram_bot_creation_error_is_catchable_as_telegram_error() -> None:
    with pytest.raises(TelegramError):
        raise TelegramBotCreationError("botfather rejected")
