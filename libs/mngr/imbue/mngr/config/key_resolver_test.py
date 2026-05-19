"""Unit tests for the shared assign-vs-extend resolver."""

from typing import Any

import pytest

from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import WorkDirExtraPathMode
from imbue.mngr.config.key_resolver import EXTEND_SUFFIX
from imbue.mngr.config.key_resolver import bare_key
from imbue.mngr.config.key_resolver import is_extend_key
from imbue.mngr.config.key_resolver import parse_scalar_value
from imbue.mngr.config.key_resolver import resolve_extends
from imbue.mngr.errors import ConfigParseError

# =============================================================================
# is_extend_key / bare_key
# =============================================================================


def test_is_extend_key_recognises_suffix() -> None:
    assert is_extend_key("cli_args__extend")
    assert is_extend_key("a__extend")


def test_is_extend_key_rejects_bare_suffix() -> None:
    """A bare ``__extend`` (no preceding field name) is not a valid extend key."""
    assert not is_extend_key(EXTEND_SUFFIX)


def test_is_extend_key_rejects_plain_field() -> None:
    assert not is_extend_key("cli_args")
    assert not is_extend_key("")


def test_bare_key_strips_suffix() -> None:
    assert bare_key("cli_args__extend") == "cli_args"
    assert bare_key("a__extend") == "a"


# =============================================================================
# parse_scalar_value
# =============================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("false", False),
        ("42", 42),
        ("3.14", 3.14),
        ('"quoted"', "quoted"),
        ("[1, 2, 3]", [1, 2, 3]),
        ('{"k": "v"}', {"k": "v"}),
        ("not_json", "not_json"),
        ("", ""),
    ],
)
def test_parse_scalar_value(raw: str, expected: Any) -> None:
    """JSON-parses first, falls back to the raw string when not valid JSON."""
    assert parse_scalar_value(raw) == expected


# =============================================================================
# resolve_extends -- list/tuple aggregate
# =============================================================================


def test_resolve_extends_appends_to_list_field() -> None:
    """__extend on a list field appends to the base value."""
    base = MngrConfig(unset_vars=["BASE_VAR"])
    resolved = resolve_extends(base, {"unset_vars__extend": ["FROM_EXTEND"]})
    assert resolved == {"unset_vars": ["BASE_VAR", "FROM_EXTEND"]}


def test_resolve_extends_assign_then_extend_in_same_layer() -> None:
    """Bare assignment is applied first; sibling __extend stacks on top.

    Concretely, `unset_vars = []` + `unset_vars__extend = ["A"]` resolves to
    ``["A"]`` -- the reset-then-add idiom called out in the spec.
    """
    base = MngrConfig(unset_vars=["OLD_BASE"])
    resolved = resolve_extends(
        base,
        {"unset_vars": [], "unset_vars__extend": ["A"]},
    )
    assert resolved == {"unset_vars": ["A"]}


def test_resolve_extends_appends_to_unset_list_field() -> None:
    """__extend with a None-valued (or absent) base falls back to using the extender directly."""
    # Use a raw dict base where the path is genuinely absent, so we hit the
    # ``current_value is None`` branch in _apply_extend.
    base: dict[str, Any] = {}
    resolved = resolve_extends(base, {"unset_vars__extend": ["NEW"]})
    assert resolved == {"unset_vars": ["NEW"]}


# =============================================================================
# resolve_extends -- dict aggregate
# =============================================================================


def test_resolve_extends_shallow_merges_dict_field() -> None:
    """__extend on a dict field shallow-merges keys; extender wins on collision."""
    base = MngrConfig(work_dir_extra_paths={".venv": WorkDirExtraPathMode.SHARE})
    resolved = resolve_extends(
        base,
        {"work_dir_extra_paths__extend": {".env": "SHARE"}},
    )
    # extender adds .env while preserving .venv from the base; values are
    # serialised through model_dump so the existing entry is rendered as its
    # JSON form ("SHARE") -- exactly what users would write in TOML.
    assert resolved == {"work_dir_extra_paths": {".venv": "SHARE", ".env": "SHARE"}}


# =============================================================================
# resolve_extends -- error cases
# =============================================================================


def test_resolve_extends_rejects_extend_on_scalar() -> None:
    """__extend on a scalar field raises ConfigParseError with a clear message."""
    base = MngrConfig(prefix="base-")
    with pytest.raises(ConfigParseError, match="__extend on field 'prefix'"):
        resolve_extends(base, {"prefix__extend": "oops"})


def test_resolve_extends_rejects_shape_mismatch_dict_on_list() -> None:
    """An object value used to extend a list field raises ConfigParseError."""
    base = MngrConfig(unset_vars=["BASE"])
    with pytest.raises(ConfigParseError, match="requires a JSON array value"):
        resolve_extends(base, {"unset_vars__extend": {"not": "a list"}})


def test_resolve_extends_rejects_scalar_for_unset_list_field() -> None:
    """__extend with a scalar value on an unset field still raises (must be aggregate).

    Use a raw dict base where the target path is absent so that the resolver
    reaches the ``current_value is None`` branch in _apply_extend.
    """
    base: dict[str, Any] = {}
    with pytest.raises(ConfigParseError, match="requires a list, tuple, dict, or set value"):
        resolve_extends(base, {"new_field__extend": "not-a-list"})


# =============================================================================
# resolve_extends -- nested paths
# =============================================================================


def test_resolve_extends_recurses_into_nested_dicts() -> None:
    """Recursion follows the override dict, applying extends at each level it appears."""
    base = MngrConfig.model_construct()
    resolved = resolve_extends(
        base,
        {"logging": {"console_level": "TRACE"}},
    )
    # No __extend keys -- the override passes through unchanged.
    assert resolved == {"logging": {"console_level": "TRACE"}}


def test_resolve_extends_walks_through_command_defaults() -> None:
    """``commands.<name>.<param>__extend`` extends against the merged value stored
    in ``CommandDefaults.defaults[<param>]`` rather than looking for a non-existent
    attribute on the model. Without this transparency, the extend would silently
    act as an assign (since the lookup would return ``None``).
    """
    base = MngrConfig(
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    resolved = resolve_extends(
        base,
        {"commands": {"create": {"env__extend": ["X=7"]}}},
    )
    assert resolved == {"commands": {"create": {"env": ["X=5", "X=7"]}}}
