import pytest

from imbue.minds.desktop_client.settings.keybindings import describe_keybindings_value_problem
from imbue.minds.desktop_client.settings.keybindings import is_canonical_chord


@pytest.mark.parametrize(
    "chord",
    ["a", "7", "[", "enter", "f11", "ctrl+a", "ctrl+shift+p", "alt+arrowleft", "ctrl+alt+shift+meta+z", "meta+plus"],
)
def test_canonical_chords_are_accepted(chord: str) -> None:
    assert is_canonical_chord(chord) is True


@pytest.mark.parametrize(
    "chord",
    [
        "",
        "A",
        "Ctrl+a",
        "cmd+a",
        "shift+ctrl+a",
        "ctrl+ctrl+a",
        "ctrl+",
        "+",
        "ctrl++",
        "ctrl + a",
        "esc",
        "ctrl",
        "a+b",
    ],
    ids=[
        "empty",
        "uppercase-key",
        "uppercase-modifier",
        "unnormalized-cmd",
        "out-of-order-modifiers",
        "repeated-modifier",
        "no-key",
        "bare-separator",
        "plus-as-literal-key",
        "spaces",
        "unknown-name",
        "modifier-as-key",
        "two-keys",
    ],
)
def test_non_canonical_chords_are_refused(chord: str) -> None:
    assert is_canonical_chord(chord) is False


def test_a_valid_keybindings_value_has_no_problem() -> None:
    value = {"files": {"rename": ["f2"], "delete": []}, "chat": {"send-message": ["enter", "ctrl+enter"]}}
    assert describe_keybindings_value_problem(value) is None
    assert describe_keybindings_value_problem({}) is None


@pytest.mark.parametrize(
    ("value", "problem_fragment"),
    [
        ("enter", "must be an object"),
        ({"Files": {}}, "not an app name"),
        ({"files": ["f2"]}, "object of actions"),
        ({"files": {"Rename!": ["f2"]}}, "not an action id"),
        ({"files": {"rename": "f2"}}, "list of chords"),
        ({"files": {"rename": ["F2"]}}, "not a canonical chord"),
    ],
    ids=["not-a-map", "bad-app-name", "no-action-level", "bad-action-id", "chords-not-a-list", "bad-chord"],
)
def test_keybindings_value_problems_are_named(value: object, problem_fragment: str) -> None:
    problem = describe_keybindings_value_problem(value)
    assert problem is not None and problem_fragment in problem
