"""Tests for telegram error hierarchy."""

from imbue.minds.errors import MindError
from imbue.minds.errors import TelegramBotCreationError
from imbue.minds.errors import TelegramCredentialError
from imbue.minds.errors import TelegramCredentialExtractionError
from imbue.minds.errors import TelegramError


def test_telegram_error_inherits_from_mind_error() -> None:
    assert issubclass(TelegramError, MindError)


def test_telegram_credential_error_inherits_from_telegram_error_and_value_error() -> None:
    assert issubclass(TelegramCredentialError, TelegramError)
    assert issubclass(TelegramCredentialError, ValueError)


def test_telegram_extraction_error_inherits_from_telegram_error() -> None:
    assert issubclass(TelegramCredentialExtractionError, TelegramError)


def test_telegram_bot_creation_error_inherits_from_telegram_error() -> None:
    assert issubclass(TelegramBotCreationError, TelegramError)
