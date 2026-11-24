import pytest
from pyinfra.connectors.local import LocalConnector
from pyinfra.connectors.ssh import SSHConnector

from imbue.mngr.hosts.pyinfra_host_builder import build_pyinfra_host


def test_local_name_selects_the_local_connector() -> None:
    pyinfra_host = build_pyinfra_host("@local")

    assert pyinfra_host.name == "@local"
    assert isinstance(pyinfra_host.connector, LocalConnector)


def test_plain_name_selects_the_ssh_connector_and_keeps_its_host_data() -> None:
    pyinfra_host = build_pyinfra_host("host.example.com", {"ssh_user": "someone", "ssh_port": 2222})

    assert pyinfra_host.name == "host.example.com"
    assert isinstance(pyinfra_host.connector, SSHConnector)
    assert pyinfra_host.data.ssh_user == "someone"
    assert pyinfra_host.data.ssh_port == 2222


def test_host_data_does_not_leak_between_hosts() -> None:
    first_host = build_pyinfra_host("first.example.com", {"ssh_user": "someone"})
    second_host = build_pyinfra_host("second.example.com")

    assert first_host.data.ssh_user == "someone"
    with pytest.raises(AttributeError, match="has no data `ssh_user`"):
        _ = second_host.data.ssh_user
