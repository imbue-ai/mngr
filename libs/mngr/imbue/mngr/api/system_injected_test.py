"""Tests for the ``<system-injected>`` sentinel wrapper."""

import pytest

from imbue.mngr.api.system_injected import wrap_system_injected
from imbue.mngr.errors import UserInputError


def test_wrap_system_injected_wraps_content() -> None:
    assert (
        wrap_system_injected("Browser foo-1 is free.", "browser-fleet")
        == '<system-injected source="browser-fleet">Browser foo-1 is free.</system-injected>'
    )


def test_wrap_system_injected_preserves_multiline_body() -> None:
    # The wrapper introduces no new newlines, so a multi-line body is preserved
    # verbatim (the transcript parser matches it with DOTALL).
    assert (
        wrap_system_injected("line one\nline two", "browser-fleet")
        == '<system-injected source="browser-fleet">line one\nline two</system-injected>'
    )


@pytest.mark.parametrize("bad_source", ["Browser-Fleet", "browser fleet", "-leading", "", "under_score"])
def test_wrap_system_injected_rejects_non_slug_source(bad_source: str) -> None:
    with pytest.raises(UserInputError):
        wrap_system_injected("hello", bad_source)
