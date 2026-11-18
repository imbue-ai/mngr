"""Integration tests for OuterHost connections over real SSH."""

import os
import resource
import shlex
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import psutil
import pytest
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import LocalFileDescriptorsExhaustedError
from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.ssh_transport import SSH_CONNECT_HANDSHAKE_RETRY_DEADLINE_SECONDS
from imbue.mngr.utils.testing import assert_runs_leave_no_descriptors_or_child_processes_behind
from imbue.mngr.utils.testing import in_process_paramiko_sshd
from imbue.mngr.utils.testing import make_client_and_host_ssh_keys
from imbue.mngr.utils.testing import write_ssh_config_reaching_port_through_proxy_command

_PROXIED_HOST_ALIAS: Final[str] = "proxied-outer-host"
_PROXIED_SESSION_COUNT: Final[int] = 5
_RELEASE_SETTLE_TIMEOUT_SECONDS: Final[float] = 10.0
# FD_SETSIZE: select() rejects any descriptor numbered at or above it.
_SELECT_DESCRIPTOR_LIMIT: Final[int] = 1024
# Room above the limit for the descriptors a connect attempt opens.
_DESCRIPTOR_HEADROOM: Final[int] = 64


def _make_outer_host_from_ssh_config(
    ssh_config_path: Path, known_hosts_path: Path, mngr_ctx: MngrContext
) -> OuterHost:
    """An outer host built like ``create_ssh_pyinfra_host_using_user_config``, but reading the given ssh_config instead of ``~/.ssh/config``."""
    host_data: dict[str, object] = {
        "ssh_config_file": str(ssh_config_path),
        "ssh_known_hosts_file": str(known_hosts_path),
        "ssh_strict_host_key_checking": "accept-new",
    }
    inventory = Inventory(([(_PROXIED_HOST_ALIAS, host_data)], {}))
    pyinfra_host = inventory.get_host(_PROXIED_HOST_ALIAS)
    pyinfra_host.init(PyinfraState(inventory=inventory))
    return OuterHost(id=HostId.generate(), connector=PyinfraConnector(pyinfra_host), mngr_ctx=mngr_ctx)


@contextmanager
def _ssh_config_reaching_a_paramiko_sshd_through_a_proxy_command(tmp_path: Path) -> Generator[Path, None, None]:
    """Run an in-process sshd and yield an ssh_config whose ``_PROXIED_HOST_ALIAS`` reaches it through a ProxyCommand."""
    client_private_key, host_private_key = make_client_and_host_ssh_keys(tmp_path)
    with in_process_paramiko_sshd(
        host_private_key, client_private_key, is_auth_held=False, dropped_connection_count=0
    ) as (port, _server):
        yield write_ssh_config_reaching_port_through_proxy_command(
            tmp_path, _PROXIED_HOST_ALIAS, port, client_private_key
        )


def _write_ssh_config_with_proxy_command(tmp_path: Path, proxy_command: str) -> Path:
    """Write an ssh_config whose ``_PROXIED_HOST_ALIAS`` goes through ``proxy_command`` and nothing else."""
    ssh_config_path = tmp_path / "ssh_config"
    ssh_config_path.write_text(f"Host {_PROXIED_HOST_ALIAS}\n  ProxyCommand {proxy_command}\n")
    return ssh_config_path


def _run_one_outer_host_session(ssh_config_path: Path, known_hosts_path: Path, mngr_ctx: MngrContext) -> None:
    """Open an outer host through ``ssh_config_path``, run a command on it, and disconnect it."""
    outer = _make_outer_host_from_ssh_config(ssh_config_path, known_hosts_path, mngr_ctx)
    result = outer.execute_idempotent_command("echo proxied-session-58213")
    outer.disconnect()
    assert result.success
    assert result.stdout.strip() == "proxied-session-58213"


@pytest.mark.timeout(60)
def test_outer_host_sessions_through_a_proxy_command_leave_no_descriptors_or_child_processes_behind(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """Each session's ProxyCommand child and its pipes are gone once the outer host disconnects."""
    known_hosts_path = tmp_path / "known_hosts"

    with _ssh_config_reaching_a_paramiko_sshd_through_a_proxy_command(tmp_path) as ssh_config_path:
        assert_runs_leave_no_descriptors_or_child_processes_behind(
            lambda: _run_one_outer_host_session(ssh_config_path, known_hosts_path, temp_mngr_ctx),
            _PROXIED_SESSION_COUNT,
        )


def _run_one_outer_host_session_whose_proxy_dies_between_commands(
    ssh_config_path: Path, known_hosts_path: Path, mngr_ctx: MngrContext
) -> None:
    """Run a command, kill the session's ProxyCommand, then run a second command, which has to reconnect."""
    this_process = psutil.Process()
    outer = _make_outer_host_from_ssh_config(ssh_config_path, known_hosts_path, mngr_ctx)
    child_pids_before_connecting = {child.pid for child in this_process.children()}
    first_result = outer.execute_idempotent_command("echo before-proxy-death-40417")
    proxy_processes = [child for child in this_process.children() if child.pid not in child_pids_before_connecting]
    for proxy_process in proxy_processes:
        proxy_process.kill()
    is_proxy_dead = poll_until(
        lambda: all(proxy_process.status() == psutil.STATUS_ZOMBIE for proxy_process in proxy_processes),
        timeout=_RELEASE_SETTLE_TIMEOUT_SECONDS,
    )
    second_result = outer.execute_idempotent_command("echo after-proxy-death-40417")
    outer.disconnect()

    assert len(proxy_processes) == 1
    assert is_proxy_dead
    assert first_result.stdout.strip() == "before-proxy-death-40417"
    assert second_result.success
    assert second_result.stdout.strip() == "after-proxy-death-40417"


@pytest.mark.timeout(60)
def test_a_connection_dropped_for_a_retry_releases_its_proxy_command_once_the_outer_host_reconnects(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A session whose ProxyCommand dies mid-session reconnects, and the dead connection's child and pipes go too.

    The retry drops the dead connection without closing it, and the reconnect replaces it.
    paramiko's reader thread never notices that a ProxyCommand hit end of file, so that
    transport stays alive until something closes it.
    """
    known_hosts_path = tmp_path / "known_hosts"

    with _ssh_config_reaching_a_paramiko_sshd_through_a_proxy_command(tmp_path) as ssh_config_path:
        assert_runs_leave_no_descriptors_or_child_processes_behind(
            lambda: _run_one_outer_host_session_whose_proxy_dies_between_commands(
                ssh_config_path, known_hosts_path, temp_mngr_ctx
            ),
            _PROXIED_SESSION_COUNT,
        )


@contextmanager
def _every_descriptor_select_can_watch_taken() -> Generator[None, None, None]:
    """Hold every descriptor number below ``select()``'s limit, so the next one this process opens is out of its range.

    That is the state a process that leaked about a thousand descriptors is in. The soft
    descriptor limit is raised for the duration if it would stop the process short of it.
    """
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed_limit = _SELECT_DESCRIPTOR_LIMIT + _DESCRIPTOR_HEADROOM
    if hard_limit != resource.RLIM_INFINITY and hard_limit < needed_limit:
        pytest.skip(f"RLIMIT_NOFILE's hard limit ({hard_limit}) is below {needed_limit}")
    if soft_limit != resource.RLIM_INFINITY and soft_limit < needed_limit:
        resource.setrlimit(resource.RLIMIT_NOFILE, (needed_limit, hard_limit))
    filler_fds: list[int] = []
    try:
        # The lowest free number is always the one handed out, so this stops once every number below the limit is held.
        while not filler_fds or filler_fds[-1] < _SELECT_DESCRIPTOR_LIMIT - 1:
            filler_fds.append(os.open(os.devnull, os.O_RDONLY))
        yield
    finally:
        for filler_fd in filler_fds:
            os.close(filler_fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))


@pytest.mark.timeout(60)
def test_a_connect_made_after_this_process_ran_out_of_descriptors_fails_at_once_as_descriptor_exhaustion(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A ProxyCommand whose pipe ``select()`` cannot watch fails the connect without a retry and names the cause.

    paramiko reports it as a banner read failure or "No existing session", either of which
    otherwise means a slow sshd worth retrying until the handshake deadline.
    """
    ssh_config_path = _write_ssh_config_with_proxy_command(tmp_path, shlex.join(["sh", "-c", "exec sleep 52817"]))
    outer = _make_outer_host_from_ssh_config(ssh_config_path, tmp_path / "known_hosts", temp_mngr_ctx)

    with _every_descriptor_select_can_watch_taken():
        started_at = time.monotonic()
        with pytest.raises(LocalFileDescriptorsExhaustedError, match="run out of file descriptors"):
            outer.connect()
        elapsed_seconds = time.monotonic() - started_at

    assert elapsed_seconds < SSH_CONNECT_HANDSHAKE_RETRY_DEADLINE_SECONDS


def _connect_outer_host_expecting_an_incompatible_banner(
    ssh_config_path: Path, known_hosts_path: Path, mngr_ctx: MngrContext
) -> None:
    outer = _make_outer_host_from_ssh_config(ssh_config_path, known_hosts_path, mngr_ctx)
    with pytest.raises(HostConnectionError, match="Incompatible version"):
        outer.connect()


@pytest.mark.timeout(60)
def test_a_failed_connect_through_a_proxy_command_releases_its_child_and_pipes_before_any_disconnect(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """A failed connect attempt's ProxyCommand is released at once, since the handshake retry's next attempt replaces it.

    The proxy answers with a banner paramiko refuses and stays running, which fails the attempt
    without retrying it.
    """
    ssh_config_path = _write_ssh_config_with_proxy_command(
        tmp_path, shlex.join(["sh", "-c", "printf 'SSH-1.0-incompatible\\r\\n'; exec sleep 75913"])
    )
    known_hosts_path = tmp_path / "known_hosts"

    assert_runs_leave_no_descriptors_or_child_processes_behind(
        lambda: _connect_outer_host_expecting_an_incompatible_banner(ssh_config_path, known_hosts_path, temp_mngr_ctx),
        1,
    )
