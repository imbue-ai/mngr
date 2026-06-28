import pytest

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError


def test_expect_success_keeps_traceback_out_of_message_but_on_stderr() -> None:
    """A failing ``mngr imbue_cloud`` subprocess must not leak its stderr traceback
    into the exception *message* (routes surface ``str(exc)`` to API callers); the
    full output is preserved on ``.stderr`` for server-side logging/debugging."""
    cli = make_fake_imbue_cloud_cli()
    traceback_stderr = (
        "Traceback (most recent call last):\n"
        '  File "/x/httpx/_transports/default.py", line 101, in map_httpcore_exceptions\n'
        "httpx.ConnectError: [Errno -2] Name or service not known\n"
    )
    result = FinishedProcess(
        returncode=1,
        stdout="",
        stderr=traceback_stderr,
        command=("mngr", "imbue_cloud", "tunnels", "list"),
        is_output_already_logged=False,
    )
    with pytest.raises(ImbueCloudCliError) as exc_info:
        cli._expect_success(result, "tunnels list")  # noqa: SLF001 - exercising the private error-surfacing path

    message = str(exc_info.value)
    assert "Traceback" not in message
    assert "httpx.ConnectError" not in message
    assert "tunnels list" in message
    # The full subprocess output is still available for server-side logging.
    assert "httpx.ConnectError" in exc_info.value.stderr
