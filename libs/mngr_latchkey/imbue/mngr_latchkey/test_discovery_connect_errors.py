import socket

import paramiko
import pytest
from paramiko.ssh_exception import NoValidConnectionsError

from imbue.mngr_latchkey.discovery import _describe_connect_errors


def _closed_loopback_port() -> int:
    """A loopback port that was just free and has no listener now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.timeout(30)
def test_a_real_refused_paramiko_connect_is_described_by_its_errno_name() -> None:
    """Pins that paramiko keeps each address's socket error, errno included, on a refused connect.

    This is the shape a desktop-local workspace whose loopback sshd port has no
    listener produces; it fails if a paramiko upgrade stops carrying the errno.
    """
    port = _closed_loopback_port()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        with pytest.raises(NoValidConnectionsError) as excinfo:
            client.connect("127.0.0.1", port=port, timeout=5, allow_agent=False, look_for_keys=False)
    finally:
        client.close()

    assert _describe_connect_errors(excinfo.value) == f"127.0.0.1:{port} ECONNREFUSED"
