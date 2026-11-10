import json
from pathlib import Path

import click
import pytest

from imbue.imbue_common.profiling import disable
from imbue.imbue_common.profiling import get_profiler
from imbue.minds.cli.run import _enable_profile_writer
from imbue.minds.cli.run import run


def test_profile_option_reads_the_env_var_the_electron_shell_sets() -> None:
    [option] = [param for param in run.params if param.name == "profile_path"]

    assert isinstance(option, click.Option)
    assert option.envvar == "MINDS_PROFILE_FILE"
    assert option.default is None


def test_enable_profile_writer_refuses_a_path_under_the_logs_directory(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    with pytest.raises(click.ClickException, match="logs directory"):
        _enable_profile_writer(log_dir / "nested" / "trace.json", log_dir)
    with pytest.raises(click.ClickException):
        _enable_profile_writer(tmp_path / "elsewhere" / ".." / "logs" / "trace.json", log_dir)

    assert get_profiler().is_enabled is False
    assert not (log_dir / "nested").exists()


def test_enable_profile_writer_starts_the_trace_file_named_after_the_run(tmp_path: Path) -> None:
    trace_path = tmp_path / "profiles" / "share.json"

    _enable_profile_writer(trace_path, tmp_path / "logs")

    assert get_profiler().is_enabled is True
    disable()
    events = json.loads(trace_path.read_text())
    assert [event["args"]["name"] for event in events if event["name"] == "process_name"] == ["minds run"]
