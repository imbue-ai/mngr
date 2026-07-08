from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from imbue.mngr.utils.model_schema import render_annotation
from imbue.mngr.utils.model_schema import walk_model_fields


def test_render_annotation_plain_class_uses_short_name() -> None:
    assert render_annotation(str) == "str"
    assert render_annotation(Path) == "Path"


def test_render_annotation_preserves_generic_parameters() -> None:
    assert render_annotation(list[str]) == "list[str]"
    assert render_annotation(dict[str, Path]) == "dict[str, Path]"
    assert render_annotation(tuple[str, ...]) == "tuple[str, ...]"


def test_render_annotation_unions_strip_module_qualifiers() -> None:
    assert render_annotation(str | None) == "str | None"
    assert render_annotation(int | str) == "int | str"


def test_render_annotation_literal() -> None:
    assert render_annotation(Literal["agent"]) == "Literal['agent']"


class _Leaf(BaseModel):
    value: int = Field(description="a value")


class _Branch(BaseModel):
    name: str = Field(description="branch name")
    leaf: _Leaf = Field(description="nested leaf")
    optional_leaf: _Leaf | None = Field(default=None, description="optional nested leaf")
    leaves: list[_Leaf] = Field(default_factory=list, description="a list of leaves")
    tags: dict[str, str] = Field(default_factory=dict, description="open-ended tags")


def test_walk_default_recurses_required_models_only_emitting_leaves() -> None:
    """Default walk recurses into a bare nested model but stops at Optional/list/dict."""
    rows = {key: (render_annotation(ann), desc) for key, ann, desc in walk_model_fields(_Branch)}
    assert "name" in rows
    # Recursed into the required nested model, but did not emit the container row.
    assert "leaf.value" in rows
    assert "leaf" not in rows
    # Optional[model] stays a leaf without recurse_optional; dict fields stop here.
    assert "optional_leaf" in rows
    assert "optional_leaf.value" not in rows
    assert rows["tags"][0] == "dict[str, str]"


def test_walk_recurse_optional_and_container_rows() -> None:
    rows = {key for key, _, _ in walk_model_fields(_Branch, recurse_optional=True, emit_container_rows=True)}
    # Container rows are emitted, and the walk recurses through Optional wrappers.
    assert "optional_leaf" in rows
    assert "optional_leaf.value" in rows
    assert "leaf" in rows
    assert "leaf.value" in rows


def test_walk_does_not_recurse_sequences_by_default() -> None:
    rows = {key for key, _, _ in walk_model_fields(_Branch, recurse_optional=True, emit_container_rows=True)}
    # The list field is a leaf row; list element fields are not dot-addressable schema paths.
    assert "leaves" in rows
    assert "leaves.value" not in rows
