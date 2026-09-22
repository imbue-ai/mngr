import json
from pathlib import Path
from typing import Final
from uuid import uuid4

import click
import pluggy
import pytest
from click.testing import CliRunner
from click.testing import Result

from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr_file.cli.get import file_get
from imbue.mngr_file.cli.list import file_list
from imbue.mngr_file.cli.put import file_put
from imbue.mngr_file.testing import AddressedMachine
from imbue.mngr_file.testing import AddressedMachineFactory
from imbue.mngr_file.testing import InteractiveStdin
from imbue.mngr_file.testing import StoppedHostFactory
from imbue.mngr_file.testing import StoppedHostStorage
from imbue.mngr_file.testing import read_tree
from imbue.mngr_file.testing import write_agent_record

_HOST_DIR_PLACEHOLDER = "<host dir>"


_STORAGE_BY_REACH: Final = {
    "running-host": None,
    "stopped-host": StoppedHostStorage.FILESYSTEM,
    "stopped-service-host": StoppedHostStorage.SERVICE,
}


def _reach_machine(reach: str, addressed_machine_factory: AddressedMachineFactory) -> AddressedMachine:
    """The machine reached the way ``reach`` names."""
    return addressed_machine_factory.create(_STORAGE_BY_REACH[reach])


def _make_directory(host_dir: Path) -> str:
    """Create a fresh directory holding ``f.txt`` under ``host_dir`` and return its relative name."""
    name = f"invariants-{uuid4().hex}"
    (host_dir / name).mkdir()
    (host_dir / name / "f.txt").write_bytes(b"existing content")
    return name


def _is_a_stated_refusal(result: Result) -> bool:
    """Whether a command's error output states one error, whatever guidance follows it."""
    return sum(line.startswith("Error: ") for line in result.stderr.splitlines()) == 1


def _is_json_record_per_line(output: str) -> bool:
    lines = output.splitlines()
    if not lines:
        return False
    try:
        return all(isinstance(json.loads(line), dict) for line in lines)
    except json.JSONDecodeError:
        return False


def _invoke_path_refusal(
    case: str, target: str, directory: str, cli_runner: CliRunner, plugin_manager: pluggy.PluginManager
) -> tuple[Result, str]:
    """Invoke a subcommand on a path it cannot use; return the result and the path as typed."""
    match case:
        case "get-missing-file":
            typed = f"{directory}/missing.txt"
            return cli_runner.invoke(file_get, [target, typed], obj=plugin_manager), typed
        case "get-directory":
            return cli_runner.invoke(file_get, [target, directory], obj=plugin_manager), directory
        case "list-missing-directory":
            typed = f"{directory}/missing"
            return cli_runner.invoke(file_list, [target, typed], obj=plugin_manager), typed
        case "list-a-file":
            typed = f"{directory}/f.txt"
            return cli_runner.invoke(file_list, [target, typed], obj=plugin_manager), typed
        case "put-onto-directory":
            return cli_runner.invoke(file_put, [target, directory], input=b"new", obj=plugin_manager), directory
        case "put-beneath-a-file":
            typed = f"{directory}/f.txt/child.txt"
            return cli_runner.invoke(file_put, [target, typed], input=b"new", obj=plugin_manager), typed
        case _:
            raise AssertionError(f"Unknown refusal case: {case}")


@pytest.mark.witnesses(
    "clean-refusals",
    partial="attacks six path conditions across get, put, and list on a running host and on stopped hosts "
    "whose storage fails as a filesystem and as a storage service; "
    "cannot cover every condition, target, and machine state a command can meet",
)
@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="checks only refusals of a path the command could not use, typed relatively",
)
@pytest.mark.parametrize(
    ("case", "stopped_reach"),
    [
        pytest.param(case, stopped_reach, id=f"{case}-{stopped_reach}")
        for case in (
            "get-missing-file",
            "get-directory",
            "list-missing-directory",
            "put-onto-directory",
            "put-beneath-a-file",
            "list-a-file",
        )
        for stopped_reach in ("stopped-host", "stopped-service-host")
    ],
)
def test_a_path_that_cannot_be_used_is_refused_with_a_stated_reason_however_the_machine_is_reached(
    case: str,
    stopped_reach: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
) -> None:
    refusals_by_reach: dict[str, str] = {}
    for reach in ("running-host", stopped_reach):
        machine = _reach_machine(reach, addressed_machine_factory)
        directory = _make_directory(machine.host_dir)
        result, typed = _invoke_path_refusal(case, machine.address, directory, cli_runner, plugin_manager)

        assert result.exit_code != 0, (reach, result.stdout)
        assert isinstance(result.exception, SystemExit), (reach, result.exception)
        assert _is_a_stated_refusal(result), (reach, result.stderr)
        assert str(machine.host_dir / typed) in result.stderr, (reach, result.stderr)
        refusals_by_reach[reach] = result.stderr.replace(str(machine.host_dir), _HOST_DIR_PLACEHOLDER).replace(
            directory, ""
        )

    assert refusals_by_reach[stopped_reach] == refusals_by_reach["running-host"]


@pytest.mark.witnesses(
    "clean-refusals",
    partial="attacks content, source, format, base, storage, target, local destination, and host directory "
    "boundary refusals, each on one machine; cannot cover every condition, target, and machine state a "
    "command can meet",
)
@pytest.mark.parametrize(
    "case",
    [
        "put-without-content",
        "put-from-a-missing-source",
        "put-from-a-directory",
        "get-saved-onto-a-directory",
        "get-saved-beneath-a-file",
        "get-with-a-template",
        "host-target-with-state-base",
        "stopped-agent-work-directory",
        "get-on-unreachable-storage",
        "put-on-unreachable-storage",
        "list-on-unreachable-storage",
        "stopped-host-path-outside-its-host-directory",
        "unknown-host",
        "unknown-agent",
    ],
)
def test_a_request_that_cannot_be_served_is_refused_with_a_stated_reason(
    case: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    stopped_host_factory: StoppedHostFactory,
    tmp_path: Path,
) -> None:
    match case:
        case "put-without-content":
            result = cli_runner.invoke(
                file_put, ["@localhost", "new.txt"], input=InteractiveStdin(), obj=plugin_manager
            )
        case "put-from-a-missing-source":
            result = cli_runner.invoke(
                file_put, ["@localhost", "new.txt", "--input", str(tmp_path / "missing")], obj=plugin_manager
            )
        case "put-from-a-directory":
            result = cli_runner.invoke(
                file_put, ["@localhost", "new.txt", "--input", str(tmp_path)], obj=plugin_manager
            )
        case "get-saved-onto-a-directory":
            typed = f"{_make_directory(temp_host_dir)}/f.txt"
            result = cli_runner.invoke(file_get, ["@localhost", typed, "--output", str(tmp_path)], obj=plugin_manager)
        case "get-saved-beneath-a-file":
            typed = f"{_make_directory(temp_host_dir)}/f.txt"
            (tmp_path / "local.txt").write_bytes(b"local")
            result = cli_runner.invoke(
                file_get,
                ["@localhost", typed, "--output", str(tmp_path / "local.txt" / "saved.txt")],
                obj=plugin_manager,
            )
        case "get-with-a-template":
            result = cli_runner.invoke(file_get, ["@localhost", "f.txt", "--format", "{path}"], obj=plugin_manager)
        case "host-target-with-state-base":
            result = cli_runner.invoke(file_list, ["@localhost", "--relative-to", "state"], obj=plugin_manager)
        case "stopped-agent-work-directory":
            host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
            agent_name = f"stopped-agent-{uuid4().hex}"
            write_agent_record(host.host_dir, agent_name, tmp_path / "work")
            result = cli_runner.invoke(file_get, [agent_name, "f.txt"], obj=plugin_manager)
        case "get-on-unreachable-storage":
            host = stopped_host_factory.create(StoppedHostStorage.UNREACHABLE)
            result = cli_runner.invoke(file_get, [host.address, "f.txt"], obj=plugin_manager)
        case "put-on-unreachable-storage":
            host = stopped_host_factory.create(StoppedHostStorage.UNREACHABLE)
            result = cli_runner.invoke(file_put, [host.address, "f.txt"], input=b"new", obj=plugin_manager)
        case "list-on-unreachable-storage":
            host = stopped_host_factory.create(StoppedHostStorage.UNREACHABLE)
            result = cli_runner.invoke(file_list, [host.address], obj=plugin_manager)
        case "stopped-host-path-outside-its-host-directory":
            host = stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)
            result = cli_runner.invoke(file_get, [host.address, str(tmp_path / "f.txt")], obj=plugin_manager)
        case "unknown-host":
            result = cli_runner.invoke(file_list, [f"@missing-{uuid4().hex}"], obj=plugin_manager)
        case "unknown-agent":
            result = cli_runner.invoke(file_get, [f"missing-{uuid4().hex}", "f.txt"], obj=plugin_manager)
        case _:
            raise AssertionError(f"Unknown refusal case: {case}")

    assert result.exit_code != 0, result.stdout
    assert isinstance(result.exception, SystemExit), result.exception
    assert _is_a_stated_refusal(result), result.stderr


@pytest.mark.witnesses(
    "resolved-path-reported",
    partial="checks one relative path in every report format of each subcommand on a running host, an agent's "
    "work directory, and a stopped host; cannot cover every path, target, and machine state",
)
@pytest.mark.parametrize(
    "report",
    [
        "put-human",
        "put-json",
        "put-jsonl",
        "put-template",
        "get-json",
        "get-jsonl",
        "get-saved-human",
        "get-saved-json",
        "list-human",
        "list-json",
        "list-jsonl",
        "list-template",
    ],
)
@pytest.mark.parametrize("reach", ["running-host", "agent-work-directory", "stopped-host"])
def test_a_report_names_the_absolute_path_of_a_relative_path(
    reach: str,
    report: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_agent: AgentInterface,
    addressed_machine_factory: AddressedMachineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if reach == "agent-work-directory":
        target, base_dir = str(local_agent.name), local_agent.work_dir
    else:
        machine = _reach_machine(reach, addressed_machine_factory)
        target, base_dir = machine.address, machine.host_dir
    directory = _make_directory(base_dir)
    typed = f"{directory}/f.txt"
    expected = str(base_dir / typed)
    monkeypatch.chdir(tmp_path)
    saved = str(Path.cwd() / "saved.txt")

    def invoke(command: click.Command, *args: str) -> str:
        result = cli_runner.invoke(command, [target, *args], input=b"new content", obj=plugin_manager)
        assert result.exit_code == 0, result.output
        return result.stdout

    match report:
        case "put-human":
            assert expected in invoke(file_put, typed)
        case "put-json":
            assert json.loads(invoke(file_put, typed, "--format", "json"))["path"] == expected
        case "put-jsonl":
            assert json.loads(invoke(file_put, typed, "--format", "jsonl"))["path"] == expected
        case "put-template":
            assert invoke(file_put, typed, "--format", "{path}") == f"{expected}\n"
        case "get-json":
            assert json.loads(invoke(file_get, typed, "--format", "json"))["path"] == expected
        case "get-jsonl":
            assert json.loads(invoke(file_get, typed, "--format", "jsonl"))["path"] == expected
        case "get-saved-human":
            saved_report = invoke(file_get, typed, "--output", "saved.txt")
            assert expected in saved_report
            assert saved in saved_report
        case "get-saved-json":
            saved_event = json.loads(invoke(file_get, typed, "--output", "saved.txt", "--format", "json"))
            assert saved_event["path"] == expected
            assert saved_event["output_path"] == saved
        case "list-human":
            assert expected in invoke(file_list, directory, "--fields", "path")
        case "list-json":
            listing = json.loads(invoke(file_list, directory, "--format", "json"))
            assert [entry["path"] for entry in listing["files"]] == [expected]
        case "list-jsonl":
            jsonl_lines = invoke(file_list, directory, "--format", "jsonl").splitlines()
            assert [json.loads(line)["path"] for line in jsonl_lines] == [expected]
        case "list-template":
            assert invoke(file_list, directory, "--format", "{path}") == f"{expected}\n"
        case _:
            raise AssertionError(f"Unknown report: {report}")


@pytest.mark.witnesses(
    "whole-files-only",
    partial="checks put over an existing longer file on three machine reaches; cannot cover every write "
    "and machine state",
)
@pytest.mark.parametrize("reach", ["running-host", "stopped-host", "stopped-service-host"])
def test_a_write_replaces_the_whole_file(
    reach: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
) -> None:
    machine = _reach_machine(reach, addressed_machine_factory)
    directory = _make_directory(machine.host_dir)

    result = cli_runner.invoke(file_put, [machine.address, f"{directory}/f.txt"], input=b"new", obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert (machine.host_dir / directory / "f.txt").read_bytes() == b"new"


@pytest.mark.witnesses(
    "whole-files-only",
    partial="checks reads to the output stream, as a record, and into a local file, and shallow and recursive "
    "listings, on three machine reaches; cannot cover every read, listing, and machine state",
)
@pytest.mark.parametrize(
    "operation", ["get-to-output", "get-as-record", "get-saved", "list-shallow", "list-recursive"]
)
@pytest.mark.parametrize("reach", ["running-host", "stopped-host", "stopped-service-host"])
def test_reading_and_listing_leave_the_machine_as_it_was(
    reach: str,
    operation: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
    tmp_path: Path,
) -> None:
    machine = _reach_machine(reach, addressed_machine_factory)
    directory = _make_directory(machine.host_dir)
    (machine.host_dir / directory / "sub").mkdir()
    (machine.host_dir / directory / "sub" / "g.txt").write_bytes(b"nested content")
    before = read_tree(machine.host_dir / directory)
    command, args = {
        "get-to-output": (file_get, [f"{directory}/f.txt"]),
        "get-as-record": (file_get, [f"{directory}/f.txt", "--format", "json"]),
        "get-saved": (file_get, [f"{directory}/f.txt", "--output", str(tmp_path / "saved.txt")]),
        "list-shallow": (file_list, [directory]),
        "list-recursive": (file_list, [directory, "--recursive"]),
    }[operation]

    result = cli_runner.invoke(command, [machine.address, *args], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert read_tree(machine.host_dir / directory) == before


@pytest.mark.witnesses(
    "named-output-formats",
    partial="checks the three format names for each subcommand on one running host; cannot cover every "
    "outcome a subcommand can report",
)
@pytest.mark.parametrize("format_name", ["human", "json", "jsonl"])
@pytest.mark.parametrize("subcommand", ["put", "get", "list"])
def test_each_subcommand_reports_in_the_format_named(
    subcommand: str,
    format_name: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = _make_directory(temp_host_dir)
    command, args = {
        "put": (file_put, [f"{directory}/f.txt"]),
        "get": (file_get, [f"{directory}/f.txt"]),
        "list": (file_list, [directory]),
    }[subcommand]

    result = cli_runner.invoke(
        command, ["@localhost", *args, "--format", format_name], input=b"not a record", obj=plugin_manager
    )

    assert result.exit_code == 0, result.output
    if format_name == "human":
        assert result.stdout != "" and not _is_json_record_per_line(result.stdout)
    else:
        assert _is_json_record_per_line(result.stdout)


@pytest.mark.witnesses(
    "named-output-formats",
    partial="checks listings of zero, one, and three entries and one write; cannot cover every outcome",
)
@pytest.mark.parametrize("outcome", ["list-of-0", "list-of-1", "list-of-3", "put"])
def test_a_template_emits_one_line_per_record(
    outcome: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
) -> None:
    directory = temp_host_dir / f"invariants-{uuid4().hex}"
    directory.mkdir()
    if outcome == "put":
        result = cli_runner.invoke(
            file_put,
            ["@localhost", f"{directory.name}/written.txt", "--format", "{path}"],
            input=b"x",
            obj=plugin_manager,
        )
        expected_line_count = 1
    else:
        expected_line_count = int(outcome.removeprefix("list-of-"))
        for index in range(expected_line_count):
            (directory / f"entry-{index}.txt").write_bytes(b"x")
        result = cli_runner.invoke(file_list, ["@localhost", directory.name, "--format", "{name}"], obj=plugin_manager)

    assert result.exit_code == 0, result.output
    assert [line.endswith("\n") for line in result.stdout.splitlines(keepends=True)] == [True] * expected_line_count


@pytest.mark.witnesses(
    "named-output-formats",
    partial="checks the path and size fields shared by put and list on a running and a stopped host; "
    "cannot cover every value a field can take",
)
@pytest.mark.parametrize("reach", ["running-host", "stopped-host"])
def test_a_field_offered_by_several_subcommands_renders_the_same_in_each(
    reach: str,
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    addressed_machine_factory: AddressedMachineFactory,
) -> None:
    machine = _reach_machine(reach, addressed_machine_factory)
    directory = f"invariants-{uuid4().hex}"
    template = "{path}|{size}"

    put_result = cli_runner.invoke(
        file_put, [machine.address, f"{directory}/f.txt", "--format", template], input=b"x" * 1500, obj=plugin_manager
    )
    list_result = cli_runner.invoke(file_list, [machine.address, directory, "--format", template], obj=plugin_manager)

    assert put_result.exit_code == 0, put_result.output
    assert list_result.exit_code == 0, list_result.output
    assert list_result.stdout == put_result.stdout
