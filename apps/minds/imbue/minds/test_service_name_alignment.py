"""Verify the embed contract's service-name wire check tracks the registry rule.

The canonical shape is
:data:`imbue.mngr_latchkey.additional_services.SERVICE_NAME_PATTERN`. The
embed contract cannot import it (plain JS served verbatim to browsers and
vendored into default-workspace-template), so it mirrors it as a charset
superset with a length cap. Drift in either direction makes registry-legal
apps silently unshareable -- the contract drops the message before any
handler runs -- so this asserts every registry-legal name up to the cap is
wire-legal.
"""

import re
import string
from pathlib import Path
from typing import Final

from imbue.mngr_latchkey.additional_services import SERVICE_NAME_PATTERN

_EMBED_CONTRACT_PATH: Final[Path] = Path(__file__).resolve().parent / "desktop_client" / "static" / "embed_contract.js"

# Matching the whole declaration line makes a rename or reshape fail loudly
# here instead of silently decoupling the two rules.
_WIRE_PATTERN_DECLARATION: Final[re.Pattern[str]] = re.compile(
    r"^export const SERVICE_NAME_PATTERN = /(?P<body>[^/]+)/;$", re.MULTILINE
)

_WIRE_LENGTH_CAP: Final[int] = 64


def _wire_pattern() -> re.Pattern[str]:
    match = _WIRE_PATTERN_DECLARATION.search(_EMBED_CONTRACT_PATH.read_text())
    assert match is not None, (
        f"SERVICE_NAME_PATTERN not found in {_EMBED_CONTRACT_PATH}; if it was renamed or reshaped, "
        "update this test alongside it."
    )
    return re.compile(match.group("body"))


def test_wire_check_accepts_every_registry_legal_name_up_to_the_cap() -> None:
    registry = re.compile(SERVICE_NAME_PATTERN)
    wire = _wire_pattern()

    # Every legal first character, alone (the shortest legal names).
    for first in string.ascii_lowercase + string.digits:
        assert registry.fullmatch(first), f"registry rejects {first!r}; charsets have drifted"
        assert wire.fullmatch(first), f"wire check rejects registry-legal name {first!r}"
    # Every legal subsequent character.
    for rest in string.ascii_lowercase + string.digits + "_-":
        name = f"a{rest}"
        assert registry.fullmatch(name), f"registry rejects {name!r}; charsets have drifted"
        assert wire.fullmatch(name), f"wire check rejects registry-legal name {name!r}"
    # The longest name the wire check carries.
    assert wire.fullmatch("a" * _WIRE_LENGTH_CAP)

    # The cap is the wire check's only narrowing: a longer name is
    # registry-legal but dropped on the wire (the cap bounds a hostile payload).
    overlong = "a" * (_WIRE_LENGTH_CAP + 1)
    assert registry.fullmatch(overlong)
    assert not wire.fullmatch(overlong)
