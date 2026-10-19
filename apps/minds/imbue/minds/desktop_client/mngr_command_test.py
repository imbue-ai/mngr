"""Unit tests for the shared ``mngr`` subprocess runners."""

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrentShutdownError
from imbue.minds.desktop_client.mngr_command import run_mngr_capturing
from imbue.minds.errors import MngrCommandError


def test_run_mngr_capturing_wraps_shutdown_launch_failure_as_mngr_command_error() -> None:
    # The group's launch precondition reports shutdown as a ConcurrencyExceptionGroup
    # (not a ConcurrencyGroupError), which must still surface as the domain error.
    with ConcurrencyGroup(name="mngr-command-test") as concurrency_group:
        concurrency_group.shutdown()
        with pytest.raises(MngrCommandError) as exc_info:
            run_mngr_capturing(concurrency_group, ["true"], env={})

    cause = exc_info.value.__cause__
    assert isinstance(cause, ConcurrencyExceptionGroup)
    assert [type(exc) for exc in cause.exceptions] == [ConcurrentShutdownError]
