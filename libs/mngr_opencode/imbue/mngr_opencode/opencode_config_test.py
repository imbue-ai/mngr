"""Unit tests for the pure opencode_config builders, readers, and path helpers."""

import importlib.resources
import json
from pathlib import Path

import pytest

from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_opencode import resources as _opencode_resources
from imbue.mngr_opencode.opencode_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_opencode.opencode_config import COMMON_TRANSCRIPT_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import COMMON_TRANSCRIPT_SOURCE
from imbue.mngr_opencode.opencode_config import LAUNCH_SCRIPT_NAME
from imbue.mngr_opencode.opencode_config import OPENCODE_BIN_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_PORT_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_WORKDIR_ENV_VAR
from imbue.mngr_opencode.opencode_config import PLUGIN_FILENAME
from imbue.mngr_opencode.opencode_config import RAW_TRANSCRIPT_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import ROLE_ENV_VAR
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import SERVER_PORT_FILENAME
from imbue.mngr_opencode.opencode_config import SERVER_ROLE
from imbue.mngr_opencode.opencode_config import build_opencode_config
from imbue.mngr_opencode.opencode_config import get_opencode_app_data_dir
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.opencode_config import get_opencode_root_session_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_server_port_file_path
from imbue.mngr_opencode.opencode_config import get_shared_opencode_auth_path
from imbue.mngr_opencode.opencode_config import read_opencode_config
from imbue.mngr_opencode.opencode_config import serialize_opencode_config


def test_build_opencode_config_always_sets_schema() -> None:
    config = build_opencode_config({}, {}, False)
    assert config["$schema"] == "https://opencode.ai/config.json"


def test_build_opencode_config_layers_base_then_overrides() -> None:
    config = build_opencode_config({"theme": "dark", "model": "old/m"}, {"model": "new/m"}, False)
    assert config["theme"] == "dark"
    assert config["model"] == "new/m"


def test_build_opencode_config_auto_allow_injects_wildcard_then_overrides_win() -> None:
    """auto-allow injects a wildcard permission; an explicit permission override still wins."""
    auto_only = build_opencode_config({}, {}, True)
    assert auto_only["permission"] == {"*": "allow"}
    overridden = build_opencode_config({}, {"permission": {"bash": "deny"}}, True)
    assert overridden["permission"] == {"bash": "deny"}


def test_build_opencode_config_does_not_mutate_base() -> None:
    base = {"theme": "dark"}
    build_opencode_config(base, {"model": "m"}, True)
    assert base == {"theme": "dark"}


def test_serialize_opencode_config_round_trips() -> None:
    config = {"model": "a/b", "permission": {"*": "allow"}}
    assert json.loads(serialize_opencode_config(config)) == config


def _local_host(local_provider: LocalProviderInstance) -> OnlineHostInterface:
    return local_provider.create_host(HostName(LOCAL_HOST_NAME))


def test_read_opencode_config_missing_file_returns_empty(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    assert read_opencode_config(_local_host(local_provider), tmp_path / "absent.json") == {}


def test_read_opencode_config_empty_file_returns_empty(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text("   \n")
    assert read_opencode_config(_local_host(local_provider), config_path) == {}


def test_read_opencode_config_valid_object(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text('{"model": "a/b"}')
    assert read_opencode_config(_local_host(local_provider), config_path) == {"model": "a/b"}


def test_read_opencode_config_malformed_json_raises(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text("{not json")
    with pytest.raises(UserInputError):
        read_opencode_config(_local_host(local_provider), config_path)


def test_read_opencode_config_non_object_raises(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text("[1, 2, 3]")
    with pytest.raises(UserInputError):
        read_opencode_config(_local_host(local_provider), config_path)


def test_path_helpers_layout(tmp_path: Path) -> None:
    state = tmp_path / "agent"
    config_dir = get_opencode_config_dir(state)
    data_home = get_opencode_data_home(state)
    assert config_dir == state / "plugin" / "opencode" / "config"
    assert data_home == state / "plugin" / "opencode" / "data"
    assert get_opencode_config_file_path(config_dir) == config_dir / "opencode.json"
    assert get_opencode_plugin_path(config_dir) == config_dir / "plugin" / PLUGIN_FILENAME
    assert get_opencode_app_data_dir(data_home) == data_home / "opencode"
    assert get_opencode_auth_path_for_data_home(data_home) == data_home / "opencode" / "auth.json"
    assert get_opencode_root_session_file_path(state) == state / ROOT_SESSION_FILENAME
    assert get_opencode_server_port_file_path(state) == state / SERVER_PORT_FILENAME


def test_shared_auth_path_under_xdg_default(tmp_path: Path) -> None:
    assert get_shared_opencode_auth_path(tmp_path) == tmp_path / ".local" / "share" / "opencode" / "auth.json"


def test_plugin_resource_literals_stay_in_sync_with_constants() -> None:
    """The TS plugin hardcodes filenames/paths/role the Python side owns; guard against drift.

    The plugin can't import opencode_config.py, so it duplicates these literals.
    If a constant changes here without the .ts being updated, the marker / raw
    capture / role guard would silently break -- this test fails loudly instead.
    """
    plugin_source = importlib.resources.files(_opencode_resources).joinpath(PLUGIN_FILENAME).read_text()
    assert f'"{ACTIVE_MARKER_FILENAME}"' in plugin_source
    assert f'"{RAW_TRANSCRIPT_RELATIVE_PATH}"' in plugin_source
    assert f'"{ROLE_ENV_VAR}"' in plugin_source
    assert f'"{SERVER_ROLE}"' in plugin_source


def test_launch_script_literals_stay_in_sync_with_constants() -> None:
    """The launch script hardcodes the file names / role / env vars the Python side owns."""
    launch_source = importlib.resources.files(_opencode_resources).joinpath(LAUNCH_SCRIPT_NAME).read_text()
    assert ROOT_SESSION_FILENAME in launch_source
    assert SERVER_PORT_FILENAME in launch_source
    assert f"{ROLE_ENV_VAR}={SERVER_ROLE}" in launch_source
    for env_var in (OPENCODE_BIN_ENV_VAR, OPENCODE_PORT_ENV_VAR, OPENCODE_WORKDIR_ENV_VAR):
        assert env_var in launch_source


def test_converter_resource_paths_stay_in_sync_with_constants() -> None:
    """The converter .sh hardcodes the raw input and common output paths; guard against drift."""
    converter_source = (
        importlib.resources.files(_opencode_resources).joinpath("opencode_common_transcript.sh").read_text()
    )
    assert RAW_TRANSCRIPT_RELATIVE_PATH in converter_source
    assert COMMON_TRANSCRIPT_RELATIVE_PATH in converter_source
    # The converter stamps this `source` on every emitted event; it hardcodes the
    # literal (it runs as standalone embedded Python), so guard it against drift.
    assert f'_SOURCE = "{COMMON_TRANSCRIPT_SOURCE}"' in converter_source
