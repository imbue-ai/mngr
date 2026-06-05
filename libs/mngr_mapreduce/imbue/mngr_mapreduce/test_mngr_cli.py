"""Integration tests for the mngr CLI subprocess wrapper.

These shell out to the real ``mngr`` entrypoint, so they live here (a
``test_*.py`` integration file) rather than alongside the pure-logic unit
tests in ``mngr_cli_test.py``.
"""

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr_mapreduce.mngr_cli import _run_mngr_raw


def test_run_mngr_raw_invokes_real_mngr_and_returns_completed_process(cg: ConcurrencyGroup) -> None:
    """``_run_mngr_raw`` shells out to mngr and returns its finished process.

    ``config list`` is a fast, network-free subcommand that always prints
    the resolved config, so a successful invocation must not time out, must
    exit 0, and must produce output on stdout.
    """
    result = _run_mngr_raw(["config", "list"], cg, timeout=30.0)
    assert result.is_timed_out is False
    assert result.returncode == 0
    assert result.stdout.strip() != ""
