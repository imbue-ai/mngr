import json
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import click
import pytest
from click.testing import CliRunner

from imbue.imbue_common.profiling import disable
from imbue.imbue_common.profiling import get_profiler
from imbue.minds.cli.run import _announce_bare_origin_sign_in
from imbue.minds.cli.run import _enable_profile_writer
from imbue.minds.cli.run import run
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import OutputFormat


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


def test_run_stops_at_startup_on_a_workspace_name_base_that_cannot_be_a_host_name() -> None:
    result = CliRunner().invoke(run, [], env={"MINDS_DEFAULT_WORKSPACE_NAME_BASE": "!!!"})

    assert result.exit_code != 0
    assert "MINDS_DEFAULT_WORKSPACE_NAME_BASE='!!!' is not a usable workspace name base" in result.output


@pytest.mark.witnesses("browser-authorization.preauth-no-code")
def test_backend_started_by_the_desktop_app_hands_over_a_preauth_cookie_and_mints_no_one_time_code(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    login_url, preauth_cookie = _announce_bare_origin_sign_in(
        auth_store=FileAuthStore(data_directory=auth_dir),
        port=8420,
        is_electron=True,
        output_format=OutputFormat.JSONL,
    )

    assert preauth_cookie is not None
    # The whole event the app reads off stdout: it refuses to start on one without the cookie.
    assert json.loads(capfd.readouterr().out) == {
        "event": "login_url",
        "login_url": login_url,
        "preauth_cookie": preauth_cookie.get_secret_value(),
    }
    assert "one_time_code" not in login_url
    assert list(auth_dir.iterdir()) == []


def test_backend_started_from_a_terminal_announces_a_one_time_login_url_and_no_preauth_cookie(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    auth_store = FileAuthStore(data_directory=auth_dir)

    login_url, preauth_cookie = _announce_bare_origin_sign_in(
        auth_store=auth_store, port=8420, is_electron=False, output_format=OutputFormat.JSONL
    )

    assert preauth_cookie is None
    event = json.loads(capfd.readouterr().out)
    assert event["login_url"] == login_url
    assert "preauth_cookie" not in event
    [code] = parse_qs(urlsplit(login_url).query)["one_time_code"]
    assert auth_store.validate_and_consume_code(code=OneTimeCode(code)) is True
    assert auth_store.validate_and_consume_code(code=OneTimeCode(code)) is False
