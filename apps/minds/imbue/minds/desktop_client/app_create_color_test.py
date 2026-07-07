"""Unit tests for the shared create-request color parse helper."""

import pytest

from imbue.minds.desktop_client.create_helpers import color_for_new_workspace
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR


def test_color_for_new_workspace_normalizes_lenient_hex() -> None:
    assert color_for_new_workspace("FFF") == "#ffffff"
    assert color_for_new_workspace("#0b292b") == "#0b292b"


@pytest.mark.parametrize(
    "raw_color",
    [
        # Absent form field / JSON field (handlers default to "").
        "",
        # Explicit JSON null (`{"color": null}`): conventionally "not
        # provided", so it must take the silent missing-color path rather
        # than being coerced to the string "None" and logged as malformed.
        None,
    ],
)
def test_color_for_new_workspace_defaults_silently_for_missing_values(raw_color: object) -> None:
    assert color_for_new_workspace(raw_color) == DEFAULT_WORKSPACE_COLOR


def test_color_for_new_workspace_defaults_for_malformed_values() -> None:
    assert color_for_new_workspace("not-a-hex") == DEFAULT_WORKSPACE_COLOR
    assert color_for_new_workspace("#ffffff80") == DEFAULT_WORKSPACE_COLOR
