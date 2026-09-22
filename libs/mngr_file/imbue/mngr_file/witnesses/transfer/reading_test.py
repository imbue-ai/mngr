import base64
import json
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.testing import AddressedMachineFactory
from imbue.mngr_file.testing import StoppedHostStorage
from imbue.mngr_file.testing import read_tree


@pytest.mark.witnesses("transfer.bytes-to-output")
@pytest.mark.witnesses(
    "whole-files-only",
    partial="only a read of one file on the running local host rendered for a human; no write or listing",
)
def test_read_puts_exactly_the_file_bytes_on_the_output_stream(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = temp_host_dir / f"read-{uuid4().hex}"
    directory.mkdir()
    content = b"first line\r\nsecond line\x00\xff without a trailing newline"
    (directory / "known.bin").write_bytes(content)
    tree_before = read_tree(directory)

    result = cli_runner.invoke(
        file_get,
        ["@localhost", f"{directory.name}/known.bin", "--relative-to", "host", "--format", "human"],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout_bytes == content
    assert read_tree(directory) == tree_before


@pytest.mark.witnesses("transfer.content-in-machine-readable-report")
@pytest.mark.witnesses(
    "whole-files-only",
    partial="only a machine-readable read of one file on the running local host; no write or listing",
)
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="only a machine-readable read of one file on the running local host addressed by a relative path",
)
@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_machine_readable_read_carries_the_exact_bytes_size_and_absolute_path(
    output_format: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = temp_host_dir / f"read-{uuid4().hex}"
    directory.mkdir()
    content = b"\xff\xfe\x00\x80 invalid utf-8 \xc3\x28 end"
    file_path = directory / "binary.bin"
    file_path.write_bytes(content)
    tree_before = read_tree(directory)

    result = cli_runner.invoke(
        file_get,
        ["@localhost", f"{directory.name}/binary.bin", "--relative-to", "host", "--format", output_format],
        obj=plugin_manager,
    )

    assert result.exit_code == 0, result.stderr
    record = json.loads(result.stdout)
    assert base64.b64decode(record["content_base64"], validate=True) == content
    assert record["size"] == len(content)
    assert record["path"] == str(file_path)
    assert read_tree(directory) == tree_before


@pytest.mark.witnesses("transfer.directory-refusal-is-machine-independent")
@pytest.mark.parametrize("stopped_storage", [StoppedHostStorage.FILESYSTEM, StoppedHostStorage.SERVICE])
def test_directory_is_refused_in_the_same_terms_on_machines_reached_differently(
    stopped_storage: StoppedHostStorage,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
) -> None:
    running_machine = addressed_machine_factory.create(None)
    stopped_machine = addressed_machine_factory.create(stopped_storage)
    directory_name = f"a-directory-{uuid4().hex}"
    local_directory = running_machine.host_dir / directory_name
    local_directory.mkdir()
    stopped_directory = stopped_machine.host_dir / directory_name
    stopped_directory.mkdir()

    local_result = cli_runner.invoke(file_get, [running_machine.address, directory_name], obj=plugin_manager)
    stopped_result = cli_runner.invoke(file_get, [stopped_machine.address, directory_name], obj=plugin_manager)

    assert local_result.exit_code != 0 and stopped_result.exit_code == local_result.exit_code
    assert isinstance(local_result.exception, SystemExit) and isinstance(stopped_result.exception, SystemExit)
    assert local_result.stderr.replace(str(local_directory), "<path>") == stopped_result.stderr.replace(
        str(stopped_directory), "<path>"
    )
