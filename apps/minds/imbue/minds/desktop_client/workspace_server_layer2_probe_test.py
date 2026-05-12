"""Unit tests for the layer-2 (container) recovery probe."""

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.workspace_server_layer2_probe import Layer2State
from imbue.minds.desktop_client.workspace_server_layer2_probe import probe_layer2
from imbue.mngr.primitives import AgentId


def _write_fake_mngr_binary(path: Path, *, stdout: str = "", returncode: int = 0) -> Path:
    """Drop a tiny shell script that mimics the `mngr exec` interface.

    The probe only cares about stdout + exit code, so the script ignores
    its arguments and just prints whatever the test wants and exits with
    the requested code.
    """
    contents = f"""#!/bin/sh
cat <<'_PROBE_OUT_EOF_'
{stdout}
_PROBE_OUT_EOF_
exit {returncode}
"""
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def concurrency_group() -> Iterator[ConcurrencyGroup]:
    with ConcurrencyGroup(name="layer2_probe_test") as group:
        yield group


def test_probe_returns_alive_when_bootstrap_window_present(
    tmp_path: Path, concurrency_group: ConcurrencyGroup
) -> None:
    """The probe is satisfied by any line of the form `<session>:svc-system_interface`."""
    fake_mngr = _write_fake_mngr_binary(
        tmp_path / "mngr",
        stdout="devminds-foo:svc-system_interface\ndevminds-foo:svc-other",
    )

    result = probe_layer2(
        mngr_binary=str(fake_mngr),
        mngr_host_dir=tmp_path,
        agent_id=AgentId.generate(),
        concurrency_group=concurrency_group,
    )

    assert result == Layer2State.ALIVE


def test_probe_returns_down_when_bootstrap_window_missing(
    tmp_path: Path, concurrency_group: ConcurrencyGroup
) -> None:
    """If tmux runs but never names the bootstrap window, treat as DOWN."""
    fake_mngr = _write_fake_mngr_binary(
        tmp_path / "mngr", stdout="devminds-foo:svc-ttyd\ndevminds-foo:editor"
    )

    result = probe_layer2(
        mngr_binary=str(fake_mngr),
        mngr_host_dir=tmp_path,
        agent_id=AgentId.generate(),
        concurrency_group=concurrency_group,
    )

    assert result == Layer2State.DOWN


def test_probe_returns_down_on_nonzero_exit(
    tmp_path: Path, concurrency_group: ConcurrencyGroup
) -> None:
    """mngr exec returning non-zero (e.g. host unreachable) is treated as DOWN."""
    fake_mngr = _write_fake_mngr_binary(tmp_path / "mngr", stdout="", returncode=1)

    result = probe_layer2(
        mngr_binary=str(fake_mngr),
        mngr_host_dir=tmp_path,
        agent_id=AgentId.generate(),
        concurrency_group=concurrency_group,
    )

    assert result == Layer2State.DOWN


def test_probe_returns_down_on_missing_binary(
    tmp_path: Path, concurrency_group: ConcurrencyGroup
) -> None:
    """A missing mngr binary (OSError on exec) classifies as DOWN, not exception."""
    result = probe_layer2(
        mngr_binary=str(tmp_path / "does-not-exist"),
        mngr_host_dir=tmp_path,
        agent_id=AgentId.generate(),
        concurrency_group=concurrency_group,
    )

    assert result == Layer2State.DOWN
