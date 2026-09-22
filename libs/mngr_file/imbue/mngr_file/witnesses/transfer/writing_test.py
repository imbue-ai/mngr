import json
import stat
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.testing import InteractiveStdin
from imbue.mngr_file.testing import read_tree


@pytest.mark.witnesses("transfer.content-from-input-stream")
def test_write_takes_its_content_from_the_input_stream(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    content = b"from the input stream\x00\xff\r\n"
    file_name = f"stdin-{uuid4().hex}.bin"

    cli_runner.invoke(file_put, ["@localhost", file_name], input=content, obj=plugin_manager)

    assert (temp_host_dir / file_name).read_bytes() == content


@pytest.mark.witnesses("transfer.content-from-local-file")
def test_write_takes_its_content_from_a_named_local_file(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"from the local file\x00\xff\r\n")
    file_name = f"from-file-{uuid4().hex}.bin"

    # Something is offered on the input stream too, so only the named file can account for the result.
    cli_runner.invoke(
        file_put,
        ["@localhost", file_name, "--input", str(source)],
        input=b"not the named file",
        obj=plugin_manager,
    )

    assert (temp_host_dir / file_name).read_bytes() == source.read_bytes()


@pytest.mark.witnesses("transfer.source-must-exist")
def test_write_naming_a_missing_local_source_is_a_usage_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    before = read_tree(temp_host_dir)

    result = cli_runner.invoke(
        file_put,
        ["@localhost", f"never-{uuid4().hex}.txt", "--input", str(tmp_path / "missing.txt")],
        input=b"offered on the input stream",
        obj=plugin_manager,
    )

    assert result.exit_code == 2
    assert read_tree(temp_host_dir) == before


@pytest.mark.witnesses("transfer.write-reported")
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="verified only for a successful put to the running local host with a relative path; "
    "refusals and other subcommands are not exercised",
)
@pytest.mark.parametrize("output_format", ["human", "json", "jsonl"])
def test_write_reports_the_bytes_written_and_the_absolute_path(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    output_format: str,
) -> None:
    content = b"reported content"
    relative_path = f"reported-{uuid4().hex}/file.txt"
    written_path = str(temp_host_dir / relative_path)

    result = cli_runner.invoke(
        file_put,
        ["@localhost", relative_path, "--format", output_format],
        input=content,
        obj=plugin_manager,
    )

    if output_format == "human":
        assert written_path in result.stdout
        assert str(len(content)) in result.stdout.replace(written_path, "").split()
    else:
        record = json.loads(result.stdout)
        assert record["size"] == len(content)
        assert record["path"] == written_path


@pytest.mark.witnesses("transfer.existing-file-replaced")
@pytest.mark.witnesses(
    "whole-files-only",
    partial="verified only for a put over a longer existing file on the running local host; "
    "reads and listings leaving the machine unchanged are not exercised",
)
def test_writing_over_an_existing_file_replaces_its_whole_content(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    target = temp_host_dir / f"existing-{uuid4().hex}.txt"
    previous_content = b"previous content, longer than what replaces it"
    target.write_bytes(previous_content)
    new_content = b"new content"

    cli_runner.invoke(file_put, ["@localhost", str(target)], input=new_content, obj=plugin_manager)

    on_disk = target.read_bytes()
    assert on_disk == new_content
    # Both an append and an overwrite that does not truncate would leave this tail behind.
    assert previous_content[len(new_content) :] not in on_disk


@pytest.mark.witnesses("transfer.remote-parents-created")
def test_writing_creates_the_directories_leading_to_the_addressed_path(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    missing_root = temp_host_dir / f"parents-{uuid4().hex}"
    target = missing_root / "middle" / "leaf" / "file.txt"
    content = b"content under new directories"

    cli_runner.invoke(file_put, ["@localhost", str(target)], input=content, obj=plugin_manager)

    assert missing_root.is_dir() and (missing_root / "middle").is_dir() and target.parent.is_dir()
    assert target.read_bytes() == content


@pytest.mark.witnesses("transfer.mode-applied")
def test_writing_to_a_running_host_applies_the_requested_permissions(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    target = temp_host_dir / f"mode-{uuid4().hex}.sh"

    cli_runner.invoke(
        file_put, ["@localhost", str(target), "--mode", "0751"], input=b"#!/bin/sh\n", obj=plugin_manager
    )

    assert stat.S_IMODE(target.stat().st_mode) == 0o751


@pytest.mark.witnesses("transfer.no-content-offered")
@pytest.mark.witnesses(
    "clean-refusals",
    partial="verified only for put with no content offered, addressing the running local host; "
    "every other refusing condition, target, and machine state is not exercised",
)
def test_write_with_no_content_offered_is_refused_with_guidance(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    # Any invocation records mngr's own logs in the host directory, so watch a fresh directory holding the addressed path.
    addressed_dir = temp_host_dir / f"no-content-{uuid4().hex}"
    addressed_dir.mkdir()
    before = read_tree(addressed_dir)

    result = cli_runner.invoke(
        file_put,
        ["@localhost", str(addressed_dir / "nested" / "file.txt")],
        input=InteractiveStdin(),
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert sum(line.startswith("Error: ") for line in result.stderr.splitlines()) == 1
    assert "stdin" in result.stderr
    assert "--input" in result.stderr
    assert read_tree(addressed_dir) == before
