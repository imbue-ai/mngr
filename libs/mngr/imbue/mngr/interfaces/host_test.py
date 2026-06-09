"""Unit tests for host interface data types."""

from pathlib import Path

import pytest

from imbue.mngr.interfaces.host import AgentTmuxOptions
from imbue.mngr.interfaces.host import NamedCommand
from imbue.mngr.interfaces.host import UploadFileSpec
from imbue.mngr.primitives import TmuxHeight
from imbue.mngr.primitives import TmuxWidth
from imbue.mngr.primitives import TmuxWindowSize


def test_agent_tmux_options_round_trips_through_data_dict() -> None:
    options = AgentTmuxOptions(
        width=TmuxWidth(2048),
        height=TmuxHeight(256),
        window_size=TmuxWindowSize.MANUAL,
    )
    assert AgentTmuxOptions.from_data_dict(options.to_data_dict()) == options


def test_agent_tmux_options_to_data_dict_is_json_native() -> None:
    options = AgentTmuxOptions(
        width=TmuxWidth(120),
        height=TmuxHeight(40),
        window_size=TmuxWindowSize.LATEST,
    )
    assert options.to_data_dict() == {"width": 120, "height": 40, "window_size": "LATEST"}


def test_agent_tmux_options_from_data_dict_defaults_to_all_none() -> None:
    # An agent created before this field existed has no "tmux" block in data.json.
    options = AgentTmuxOptions.from_data_dict(None)
    assert options.width is None
    assert options.height is None
    assert options.window_size is None


# === UploadFileSpec Tests ===


def test_upload_file_spec_from_string_parses_local_remote_pair() -> None:
    spec = UploadFileSpec.from_string("/local/path:/remote/path")
    assert spec.local_path == Path("/local/path")
    assert spec.remote_path == Path("/remote/path")


def test_upload_file_spec_from_string_handles_whitespace() -> None:
    spec = UploadFileSpec.from_string("  /local/path  :  /remote/path  ")
    assert spec.local_path == Path("/local/path")
    assert spec.remote_path == Path("/remote/path")


def test_upload_file_spec_from_string_raises_without_colon() -> None:
    with pytest.raises(ValueError, match="LOCAL:REMOTE format"):
        UploadFileSpec.from_string("/just/a/path")


# === NamedCommand Tests ===


def test_named_command_from_string_parses_plain_command() -> None:
    cmd = NamedCommand.from_string("npm run dev")
    assert str(cmd.command) == "npm run dev"
    assert cmd.window_name is None


def test_named_command_from_string_parses_named_command_with_double_quotes() -> None:
    cmd = NamedCommand.from_string('server="npm run dev"')
    assert str(cmd.command) == "npm run dev"
    assert cmd.window_name == "server"


def test_named_command_from_string_parses_named_command_with_single_quotes() -> None:
    cmd = NamedCommand.from_string("tests='npm test --watch'")
    assert str(cmd.command) == "npm test --watch"
    assert cmd.window_name == "tests"


def test_named_command_from_string_parses_unquoted_lowercase_name() -> None:
    # Lowercase name=command is treated as a named command (shell strips quotes)
    cmd = NamedCommand.from_string("build=make")
    assert str(cmd.command) == "make"
    assert cmd.window_name == "build"


def test_named_command_from_string_parses_unquoted_mixed_case_name() -> None:
    # Mixed-case names with underscores are treated as window names
    cmd = NamedCommand.from_string("reviewer_1=claude --dangerously-skip-permissions")
    assert str(cmd.command) == "claude --dangerously-skip-permissions"
    assert cmd.window_name == "reviewer_1"


def test_named_command_from_string_treats_uppercase_as_env_var() -> None:
    # ALL_UPPERCASE names are treated as env var assignments, not window names
    cmd = NamedCommand.from_string("FOO=bar npm run dev")
    assert str(cmd.command) == "FOO=bar npm run dev"
    assert cmd.window_name is None


def test_named_command_from_string_handles_equals_in_quoted_command() -> None:
    cmd = NamedCommand.from_string('server="FOO=bar npm run dev"')
    assert str(cmd.command) == "FOO=bar npm run dev"
    assert cmd.window_name == "server"
