from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.utils.testing import ExecOnlyParamikoServer
from imbue.mngr.utils.testing import in_process_paramiko_sshd
from imbue.mngr.utils.testing import make_client_and_host_ssh_keys
from imbue.mngr.utils.testing import make_local_ssh_host_factory


@pytest.fixture
def paramiko_ssh_host_factory(
    temp_host_dir: Path, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> Generator[tuple[Callable[[str], Host], ExecOnlyParamikoServer], None, None]:
    """SSH hosts backed by an in-process paramiko server that runs exec requests through the local shell.

    Yields (factory, server): the factory creates hosts by name, all on the one
    server, and the server records every command it was asked to run. Needs
    neither an sshd nor an ssh-keygen binary, so it runs wherever the unit tests do.
    """
    private_key_path, host_key_path = make_client_and_host_ssh_keys(tmp_path)
    with in_process_paramiko_sshd(host_key_path, private_key_path, is_auth_held=False, dropped_connection_count=0) as (
        port,
        server,
    ):
        factory = make_local_ssh_host_factory(
            port, host_key_path, private_key_path, temp_host_dir, temp_mngr_ctx, tmp_path
        )
        yield factory, server


@pytest.fixture
def source_and_work_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create source and work directories for work_dir_extra_paths tests.

    Returns (source_dir, work_dir), both already created under tmp_path.
    """
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return (source_dir, work_dir)
