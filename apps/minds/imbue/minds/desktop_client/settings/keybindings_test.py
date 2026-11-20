import re
from pathlib import Path

import pytest

from imbue.minds.desktop_client.settings.keybindings import IMBUE_STUDIO_KEYBINDINGS_APP
from imbue.minds.desktop_client.settings.keybindings import MAX_CHORDS_PER_ACTION
from imbue.minds.desktop_client.settings.keybindings import describe_keybindings_value_problem
from imbue.minds.desktop_client.settings.keybindings import describe_keybindings_write_problem
from imbue.minds.desktop_client.settings.keybindings import is_canonical_chord
from imbue.minds.desktop_client.settings.keybindings import merge_keybindings_write
from imbue.minds.desktop_client.settings.keybindings import usable_keybindings
from imbue.minds.desktop_client.settings.keybindings import with_stored_imbue_studio_keybindings


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
        ({"files": {"rename": ["alt+meta+delete"]}}, "belongs to the operating system"),
        ({"files": {"rename": [f"meta+{n}" for n in range(10)] + list("abcdefg")}}, "more than 16 chords"),
    ],
    ids=[
        "not-a-map",
        "bad-app-name",
        "no-action-level",
        "bad-action-id",
        "chords-not-a-list",
        "bad-chord",
        "refused-chord",
        "too-many-chords",
    ],
)
def test_keybindings_value_problems_are_named(value: object, problem_fragment: str) -> None:
    problem = describe_keybindings_value_problem(value)
    assert problem is not None and problem_fragment in problem


def test_the_imbue_studio_app_name_is_a_valid_app_name() -> None:
    assert describe_keybindings_value_problem({IMBUE_STUDIO_KEYBINDINGS_APP: {"quit": ["meta+q"]}}) is None


def test_a_keybindings_write_may_remove_an_action_with_null() -> None:
    write = {"imbue-studio": {"quit": None, "new-window": ["shift+meta+n"], "zoom-in": []}}
    assert describe_keybindings_write_problem(write) is None


@pytest.mark.parametrize(
    ("write", "problem_fragment"),
    [
        (["quit"], "must be an object"),
        ({"imbue-studio": {"Quit!": None}}, "not an action id"),
        ({"imbue-studio": {"quit": "meta+q"}}, "list of chords"),
        ({"imbue-studio": {"quit": ["Meta+Q"]}}, "not a canonical chord"),
        ({"imbue-studio": {"quit": ["alt+meta+delete"]}}, "belongs to the operating system"),
    ],
    ids=["not-a-map", "removal-with-bad-action-id", "chords-not-a-list", "bad-chord", "refused-chord"],
)
def test_keybindings_write_problems_are_named(write: object, problem_fragment: str) -> None:
    problem = describe_keybindings_write_problem(write)
    assert problem is not None and problem_fragment in problem


def test_a_merged_write_sets_turns_off_and_removes_actions_and_keeps_the_rest() -> None:
    stored = {
        "files": {"rename": ("f2",)},
        "imbue-studio": {"quit": ("shift+meta+q",), "zoom-in": ("meta+plus",), "zoom-out": ("meta+_",)},
    }
    write = {"imbue-studio": {"quit": None, "zoom-in": [], "new-window": ["alt+meta+n"]}}

    merged = merge_keybindings_write(stored, write)

    assert merged == {
        "files": {"rename": ["f2"]},
        "imbue-studio": {"zoom-in": [], "zoom-out": ["meta+_"], "new-window": ["alt+meta+n"]},
    }


def test_a_merged_write_drops_an_app_left_with_no_assignment() -> None:
    stored = {"files": {"rename": ("f2",)}, "imbue-studio": {"quit": ("shift+meta+q",)}}

    merged = merge_keybindings_write(stored, {"imbue-studio": {"quit": None}})

    assert merged == {"files": {"rename": ["f2"]}}


def test_a_merged_write_adds_an_app_the_stored_map_lacks() -> None:
    merged = merge_keybindings_write({}, {"imbue-studio": {"quit": ["shift+meta+q"], "new-window": None}})

    assert merged == {"imbue-studio": {"quit": ["shift+meta+q"]}}


def test_a_workspace_write_keeps_the_stored_imbue_studio_entry_over_its_own() -> None:
    written = {"chat": {"send-message": ["meta+enter"]}, "imbue-studio": {"quit": ["meta+q"]}}
    stored = {"files": {"rename": ("f2",)}, "imbue-studio": {"quit": ("shift+meta+q",)}}

    assert with_stored_imbue_studio_keybindings(written, stored) == {
        "chat": {"send-message": ["meta+enter"]},
        "imbue-studio": {"quit": ["shift+meta+q"]},
    }


def test_a_workspace_write_cannot_add_an_imbue_studio_entry_when_none_is_stored() -> None:
    written = {"chat": {"send-message": ["meta+enter"]}, "imbue-studio": {"quit": ["meta+q"]}}

    assert with_stored_imbue_studio_keybindings(written, {}) == {"chat": {"send-message": ["meta+enter"]}}


def test_the_chord_limit_matches_the_embed_contracts() -> None:
    contract_source = (Path(__file__).parents[1] / "static" / "embed_contract.js").read_text(encoding="utf-8")

    match = re.search(r"^export const MAX_CHORDS_PER_ACTION = (\d+);$", contract_source, re.MULTILINE)

    assert match is not None
    assert int(match.group(1)) == MAX_CHORDS_PER_ACTION


def test_usable_keybindings_drop_what_no_binding_can_use() -> None:
    stored = {
        "files": {"rename": ["f2", "alt+meta+delete", "Ctrl+R"], "delete": ["alt+meta+delete"], "Bad!": ["f3"]},
        "Bad App": {"x": ["f4"]},
        "chat": {"send-message": [f"meta+{n}" for n in range(10)] + list("abcdefg")},
    }

    assert usable_keybindings(stored) == {
        "files": {"rename": ["f2"], "delete": []},
        "chat": {"send-message": [f"meta+{n}" for n in range(10)] + list("abcdef")},
    }
