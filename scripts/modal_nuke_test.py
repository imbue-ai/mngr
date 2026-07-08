import pytest

from scripts.modal_nuke import ModalSchemaError
from scripts.modal_nuke import _get_app_id
from scripts.modal_nuke import _get_volume_name


def test_get_app_id_reads_modal_key() -> None:
    assert _get_app_id({"App ID": "ap-123", "Description": "demo"}) == "ap-123"


def test_get_volume_name_reads_modal_key() -> None:
    assert _get_volume_name({"Name": "vol-abc", "Created at": "today"}) == "vol-abc"


def test_get_app_id_raises_on_unexpected_schema() -> None:
    # A destructive tool must never fall back to a placeholder id; if Modal renames
    # the key, we fail loudly naming the unexpected schema instead of nuking "unknown".
    with pytest.raises(ModalSchemaError) as exc_info:
        _get_app_id({"app_id": "ap-123"})
    message = str(exc_info.value)
    assert "App ID" in message
    assert "app_id" in message


def test_get_volume_name_raises_on_unexpected_schema() -> None:
    with pytest.raises(ModalSchemaError) as exc_info:
        _get_volume_name({"name": "vol-abc"})
    message = str(exc_info.value)
    assert "Name" in message
    assert "name" in message
