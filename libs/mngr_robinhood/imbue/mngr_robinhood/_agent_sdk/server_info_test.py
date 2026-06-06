import json

from imbue.mngr_robinhood._agent_sdk.server_info import build_server_info
from imbue.mngr_robinhood._agent_sdk.server_info import find_init_event


def test_find_init_event_returns_first_system_init() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "other"}),
            json.dumps({"type": "system", "subtype": "init", "slash_commands": ["/clear"], "output_style": "concise"}),
            json.dumps({"type": "assistant", "message": {}}),
        ]
    )
    init = find_init_event(stdout)
    assert init is not None
    assert init["output_style"] == "concise"


def test_find_init_event_skips_malformed_lines_and_returns_none_when_absent() -> None:
    assert find_init_event("not json\n{}\n") is None


def test_build_server_info_maps_slash_commands_to_commands() -> None:
    info = build_server_info(
        {"type": "system", "subtype": "init", "slash_commands": ["/clear", "/help"], "output_style": "concise"}
    )
    assert info["commands"] == ["/clear", "/help"]
    assert info["output_style"] == "concise"


def test_build_server_info_defaults_when_no_init_event() -> None:
    info = build_server_info(None)
    assert info["commands"] == []
    assert info["output_style"] == "default"


def test_build_server_info_tolerates_missing_or_wrong_typed_fields() -> None:
    info = build_server_info({"type": "system", "subtype": "init"})
    assert info["commands"] == []
    assert info["output_style"] == "default"
    assert info["tools"] == []
