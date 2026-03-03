"""Tests for model field path utilities."""

import pytest
from pydantic import BaseModel
from pydantic import Field

from imbue.imbue_common.errors import SwitchError
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.utils.model_field_utils import InvalidFieldPathError
from imbue.mng.utils.model_field_utils import resolve_model_type
from imbue.mng.utils.model_field_utils import validate_field_path

# =============================================================================
# Tests for resolve_model_type
# =============================================================================


class _InnerModel(BaseModel):
    """Test model for nesting."""

    value: str = Field(description="test")


def test_resolve_model_type_returns_direct_model() -> None:
    assert resolve_model_type(_InnerModel) is _InnerModel


def test_resolve_model_type_unwraps_optional() -> None:
    assert resolve_model_type(_InnerModel | None) is _InnerModel


def test_resolve_model_type_unwraps_list() -> None:
    assert resolve_model_type(list[_InnerModel]) is _InnerModel


def test_resolve_model_type_unwraps_tuple() -> None:
    assert resolve_model_type(tuple[_InnerModel, ...]) is _InnerModel


def test_resolve_model_type_returns_none_for_dict() -> None:
    assert resolve_model_type(dict[str, str]) is None


def test_resolve_model_type_returns_none_for_primitive() -> None:
    assert resolve_model_type(str) is None
    assert resolve_model_type(int) is None


def test_resolve_model_type_raises_on_non_optional_union() -> None:
    with pytest.raises(SwitchError, match="Cannot resolve non-optional union"):
        resolve_model_type(str | int)


def test_resolve_model_type_raises_on_fixed_length_tuple() -> None:
    with pytest.raises(SwitchError, match="Expected tuple"):
        resolve_model_type(tuple[str, int])


# =============================================================================
# Tests for validate_field_path
# =============================================================================


def test_validate_field_path_accepts_valid_top_level_field() -> None:
    validate_field_path(model=AgentInfo, field_path="name")
    validate_field_path(model=AgentInfo, field_path="state")
    validate_field_path(model=AgentInfo, field_path="create_time")


def test_validate_field_path_accepts_valid_host_field() -> None:
    validate_field_path(model=AgentInfo, field_path="host.name")
    validate_field_path(model=AgentInfo, field_path="host.state")
    validate_field_path(model=AgentInfo, field_path="host.provider_name")


def test_validate_field_path_accepts_dict_subkeys() -> None:
    validate_field_path(model=AgentInfo, field_path="labels.project")
    validate_field_path(model=AgentInfo, field_path="plugin.chat_history.messages")
    validate_field_path(model=AgentInfo, field_path="host.tags.env")
    validate_field_path(model=AgentInfo, field_path="host.plugin.aws.iam_user")


def test_validate_field_path_accepts_deep_host_fields() -> None:
    validate_field_path(model=AgentInfo, field_path="host.resource.cpu.count")
    validate_field_path(model=AgentInfo, field_path="host.ssh.host")


def test_validate_field_path_rejects_unknown_top_level_field() -> None:
    with pytest.raises(InvalidFieldPathError, match="'akldfsdkfjdklfj' is not a valid field"):
        validate_field_path(model=AgentInfo, field_path="akldfsdkfjdklfj")


def test_validate_field_path_rejects_unknown_host_field() -> None:
    with pytest.raises(InvalidFieldPathError, match="'nonexistent_field' is not a valid field"):
        validate_field_path(model=AgentInfo, field_path="host.nonexistent_field")


def test_validate_field_path_rejects_unknown_deep_field() -> None:
    with pytest.raises(InvalidFieldPathError, match="'nonexistent' is not a valid field"):
        validate_field_path(model=AgentInfo, field_path="host.resource.nonexistent")
    with pytest.raises(InvalidFieldPathError, match="'bogus' is not a valid field"):
        validate_field_path(model=AgentInfo, field_path="host.resource.cpu.bogus")


def test_validate_field_path_error_includes_valid_fields() -> None:
    with pytest.raises(InvalidFieldPathError) as exc_info:
        validate_field_path(model=AgentInfo, field_path="nonexistent")
    assert "name" in str(exc_info.value)
    assert exc_info.value.invalid_segment == "nonexistent"
    assert "name" in exc_info.value.valid_fields
