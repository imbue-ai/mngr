import pytest

from imbue.mngr.agents.output_styles import parse_output_style_name
from imbue.mngr.agents.output_styles import resolve_output_style
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import OutputStyleName

_STYLED_FILE = """---
name: Engineering Subordinate
description: Concise and direct.
---
# Engineering Subordinate

Speak like a subordinate.
"""

_OTHER_STYLE_FILE = """---
name: Explanatory
---
Explain things at length.
"""

_NO_FRONTMATTER_FILE = "Just a README that happens to live here.\n"


def test_parse_output_style_name_reads_the_frontmatter_name() -> None:
    assert parse_output_style_name(_STYLED_FILE) == OutputStyleName("Engineering Subordinate")


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(_NO_FRONTMATTER_FILE, id="no_frontmatter"),
        pytest.param("---\ndescription: nameless\n---\nbody\n", id="frontmatter_without_name"),
        pytest.param("---\nname: '   '\n---\nbody\n", id="blank_name"),
    ],
)
def test_parse_output_style_name_returns_none_when_there_is_no_usable_name(contents: str) -> None:
    """A directory may hold non-style files; those are skipped, not errors."""
    assert parse_output_style_name(contents) is None


def test_resolve_output_style_returns_the_body_verbatim_including_frontmatter() -> None:
    """Agent types that fold a style into a system prompt pass the body through
    unchanged, so the frontmatter block must survive resolution."""
    resolved = resolve_output_style(
        OutputStyleName("Engineering Subordinate"),
        {"styles/engineering-subordinate.md": _STYLED_FILE, "styles/explanatory.md": _OTHER_STYLE_FILE},
    )
    assert resolved == _STYLED_FILE


def test_resolve_output_style_matches_on_frontmatter_not_filename() -> None:
    resolved = resolve_output_style(
        OutputStyleName("Engineering Subordinate"),
        {"styles/totally-unrelated-filename.md": _STYLED_FILE},
    )
    assert resolved == _STYLED_FILE


def test_resolve_output_style_ignores_files_with_no_style_name() -> None:
    resolved = resolve_output_style(
        OutputStyleName("Engineering Subordinate"),
        {"styles/README.md": _NO_FRONTMATTER_FILE, "styles/engineering-subordinate.md": _STYLED_FILE},
    )
    assert resolved == _STYLED_FILE


def test_resolve_output_style_rejects_an_unknown_name_and_lists_what_exists() -> None:
    """A silent miss would launch an unstyled agent with no signal, so this must raise."""
    with pytest.raises(UserInputError) as exc_info:
        resolve_output_style(
            OutputStyleName("Nonexistent"),
            {"styles/engineering-subordinate.md": _STYLED_FILE, "styles/explanatory.md": _OTHER_STYLE_FILE},
        )
    message = str(exc_info.value)
    assert "'Engineering Subordinate'" in message
    assert "'Explanatory'" in message


def test_resolve_output_style_reports_when_no_styles_are_defined_at_all() -> None:
    with pytest.raises(UserInputError) as exc_info:
        resolve_output_style(OutputStyleName("Engineering Subordinate"), {"styles/README.md": _NO_FRONTMATTER_FILE})
    assert "no output styles are defined" in str(exc_info.value)
