"""Unit tests for host interface data types."""

from pathlib import Path

import pytest

from imbue.mng.interfaces.host import FileModificationSpec
from imbue.mng.interfaces.host import NamedCommand
from imbue.mng.interfaces.host import UploadFileSpec

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


# === FileModificationSpec Tests ===


def test_file_modification_spec_from_string_parses_remote_text_pair() -> None:
    spec = FileModificationSpec.from_string("/remote/file:some text content")
    assert spec.remote_path == Path("/remote/file")
    assert spec.text == "some text content"


def test_file_modification_spec_from_string_handles_whitespace_in_path() -> None:
    spec = FileModificationSpec.from_string("  /remote/file  :text")
    assert spec.remote_path == Path("/remote/file")
    assert spec.text == "text"


def test_file_modification_spec_from_string_preserves_text_with_colons() -> None:
    spec = FileModificationSpec.from_string("/remote/file:text:with:colons")
    assert spec.remote_path == Path("/remote/file")
    assert spec.text == "text:with:colons"


def test_file_modification_spec_from_string_raises_without_colon() -> None:
    with pytest.raises(ValueError, match="REMOTE:TEXT format"):
        FileModificationSpec.from_string("/just/a/path")


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
