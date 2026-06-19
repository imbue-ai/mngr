"""Unit tests for the externally-owned-settings merge surface (``__mngr_merge``).

Covers the desugar of the Claude-compatible ``__mngr_merge`` map into the internal suffix
form, and the provision-time fold (``apply_settings_patch``) including the narrowing
remediation that spells out the exact ``__mngr_merge`` patch to add.
"""

from typing import Any

import pytest

from imbue.mngr.config.external_settings import apply_settings_patch
from imbue.mngr.config.external_settings import desugar_settings_overrides
from imbue.mngr.errors import ConfigParseError

# The settings_overrides root path the desugar is always handed.
_ROOT = ("agent_types", "my_claude", "settings_overrides")


def _desugar(body: dict[str, Any]) -> dict[str, Any]:
    return desugar_settings_overrides(body, _ROOT)


# =============================================================================
# desugar: __mngr_merge surface -> internal suffix form
# =============================================================================


def test_absent_map_leaves_keys_bare() -> None:
    """With no ``__mngr_merge`` map, every key stays bare (a narrowing-checked assign)."""
    assert _desugar({"model": "opus", "permissions": {"allow": ["X"]}}) == {
        "model": "opus",
        "permissions": {"allow": ["X"]},
    }


def test_extend_marks_leaf_and_ancestors() -> None:
    """An ``extend`` directive suffixes the targeted leaf and every ancestor with ``__extend``
    so the recursive merge reaches the leaf, leaving unrelated siblings bare."""
    assert _desugar(
        {"permissions": {"allow": ["X"], "deny": ["Y"]}, "__mngr_merge": {"permissions.allow": "extend"}}
    ) == {"permissions__extend": {"allow__extend": ["X"], "deny": ["Y"]}}


def test_assign_on_top_level_key_has_no_ancestor_suffix() -> None:
    """An ``assign`` on a top-level key suffixes only that key (no ancestor to mark)."""
    assert _desugar({"permissions": {"allow": ["X"]}, "__mngr_merge": {"permissions": "assign"}}) == {
        "permissions__assign": {"allow": ["X"]}
    }


def test_multiple_directives_share_ancestor() -> None:
    """Two directives under one ancestor both mark it ``__extend`` (no conflict)."""
    assert _desugar(
        {
            "permissions": {"allow": ["X"], "deny": ["Y"]},
            "__mngr_merge": {"permissions.allow": "extend", "permissions.deny": "assign"},
        }
    ) == {"permissions__extend": {"allow__extend": ["X"], "deny__assign": ["Y"]}}


def test_deeply_nested_path_marks_every_ancestor() -> None:
    """A 3-level directive marks all intermediate ancestors ``__extend`` and the leaf with its op."""
    assert _desugar({"a": {"b": {"c": [1]}}, "__mngr_merge": {"a.b.c": "extend"}}) == {
        "a__extend": {"b__extend": {"c__extend": [1]}}
    }


def test_rejects_suffix_keys_naming_the_agent_type() -> None:
    """Raw suffixes are rejected, and the error names the owning agent type (findable) and the
    ``__mngr_merge`` replacement."""
    with pytest.raises(ConfigParseError, match="my_claude.*operator suffix.*not allowed.*__mngr_merge"):
        _desugar({"permissions": {"allow__extend": ["X"]}})


def test_rejects_nested_directive_map() -> None:
    with pytest.raises(ConfigParseError, match="only allowed at the root"):
        _desugar({"permissions": {"__mngr_merge": {"allow": "extend"}}})


def test_rejects_dangling_directive() -> None:
    with pytest.raises(ConfigParseError, match="is not set there"):
        _desugar({"permissions": {"allow": ["X"]}, "__mngr_merge": {"env": "extend"}})


def test_rejects_directive_path_through_non_dict() -> None:
    """A directive path that passes through a scalar (``model.foo``) is a dangling target."""
    with pytest.raises(ConfigParseError, match="is not set there"):
        _desugar({"model": "opus", "__mngr_merge": {"model.foo": "extend"}})


def test_rejects_unknown_operator() -> None:
    with pytest.raises(ConfigParseError, match="not a valid operator"):
        _desugar({"permissions": {"allow": ["X"]}, "__mngr_merge": {"permissions.allow": "merge"}})


def test_rejects_non_mapping_directives() -> None:
    with pytest.raises(ConfigParseError, match="must be a map"):
        _desugar({"permissions": {"allow": ["X"]}, "__mngr_merge": ["permissions.allow"]})


def test_rejects_inconsistent_directives() -> None:
    """Assigning an ancestor while extending its descendant is contradictory, named by path."""
    with pytest.raises(ConfigParseError, match="inconsistent at .*permissions"):
        _desugar(
            {"permissions": {"allow": ["X"]}, "__mngr_merge": {"permissions": "assign", "permissions.allow": "extend"}}
        )


# =============================================================================
# apply_settings_patch: fold + narrowing remediation
# =============================================================================


def test_extend_merges_onto_base() -> None:
    """A desugared ``extend`` merges the override list onto the base list."""
    base = {"permissions": {"allow": ["A"]}}
    override = _desugar({"permissions": {"allow": ["B"]}, "__mngr_merge": {"permissions.allow": "extend"}})
    result = apply_settings_patch(base, override, allow_narrowing=False, base_description="home")
    assert result["permissions"]["allow"] == ["A", "B"]


def test_strips_mngr_merge_key_from_base() -> None:
    """A ``__mngr_merge`` key in the base is dropped (no-op on the floor; ignored by the CLI)."""
    base = {"model": "Base", "__mngr_merge": {"model": "extend"}}
    result = apply_settings_patch(base, {}, allow_narrowing=False, base_description="home")
    assert "__mngr_merge" not in result
    assert result["model"] == "Base"


def test_bare_override_narrows_raises() -> None:
    base = {"permissions": {"allow": ["A"]}}
    with pytest.raises(ConfigParseError, match="Settings narrowing detected"):
        apply_settings_patch(base, {"permissions": {"allow": ["B"]}}, allow_narrowing=False, base_description="home")


def test_allow_narrowing_bypasses_guard() -> None:
    base = {"permissions": {"allow": ["A"]}}
    result = apply_settings_patch(
        base, {"permissions": {"allow": ["B"]}}, allow_narrowing=True, base_description="home"
    )
    assert result["permissions"] == {"allow": ["B"]}


def test_remediation_assigns_replaced_leaf_list() -> None:
    """A pure leaf-list replacement is remediated as ``assign`` (keep the user's exact list)."""
    base = {"permissions": {"allow": ["A"]}}
    with pytest.raises(ConfigParseError) as exc_info:
        apply_settings_patch(base, {"permissions": {"allow": ["B"]}}, allow_narrowing=False, base_description="home")
    assert '__mngr_merge = {"permissions.allow" = "assign"}' in str(exc_info.value)


def test_remediation_recurses_past_dict_drop_to_nested_leaf() -> None:
    """The full nested patch in one error: a dict that drops a sibling key is ``extend`` (keep
    the sibling) and the replaced nested list is ``assign`` (keep the exact list) -- the
    remediation recurses past the dict level rather than reporting only the dict."""
    base = {"permissions": {"allow": ["A"], "deny": ["D"]}}
    # The override drops deny and replaces allow.
    override = {"permissions": {"allow": ["B"]}}
    with pytest.raises(ConfigParseError) as exc_info:
        apply_settings_patch(base, override, allow_narrowing=False, base_description="home")
    assert '__mngr_merge = {"permissions" = "extend", "permissions.allow" = "assign"}' in str(exc_info.value)


def test_remediation_skips_keys_containing_a_literal_dot() -> None:
    """A base key containing a literal dot cannot be targeted by a dotted ``__mngr_merge`` path,
    so it is omitted from the suggested patch rather than mis-advised."""
    base = {"mcpServers": {"my.server": {"allow": ["A"]}}}
    override = {"mcpServers": {"my.server": {"allow": ["B"]}}}
    with pytest.raises(ConfigParseError) as exc_info:
        apply_settings_patch(base, override, allow_narrowing=False, base_description="home")
    message = str(exc_info.value)
    # No directive is emitted for the dotted key (it can't be targeted by a dotted path), so
    # the remediation falls back to its generic guidance rather than naming the key.
    assert "Declare the affected keys in a top-level" in message
    assert '"mcpServers.my.server"' not in message
