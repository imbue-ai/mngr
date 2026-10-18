from ipaddress import IPv4Address

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr_docker.config import DockerProviderConfig
from imbue.mngr_docker.published_ports import build_published_port_publish_specs
from imbue.mngr_docker.published_ports import choose_host_ports_for_shared_guest_ports
from imbue.mngr_docker.published_ports import read_published_endpoints_from_port_bindings
from imbue.mngr_docker.testing import make_docker_provider

_UDP_59000 = PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.UDP)
_TCP_59000 = PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.TCP)
_TCP_8080 = PublishedPortDeclaration(guest_port=8080, protocol=PortProtocol.TCP)


def _config(host: str, ssh_bind_address: IPv4Address | None = None) -> DockerProviderConfig:
    return DockerProviderConfig(isolate_host_volumes=False, host=host, ssh_bind_address=ssh_bind_address)


@pytest.mark.parametrize(
    ("config", "expected_specs"),
    [
        (_config(""), ["127.0.0.1::59000/udp", "127.0.0.1:40000:8080/tcp"]),
        (_config("ssh://user@myserver"), [":59000/udp", "40000:8080/tcp"]),
        (_config("", IPv4Address("0.0.0.0")), ["0.0.0.0::59000/udp", "0.0.0.0:40000:8080/tcp"]),
    ],
)
def test_build_published_port_publish_specs_follows_the_sshd_publish_address(
    config: DockerProviderConfig, expected_specs: list[str]
) -> None:
    specs = build_published_port_publish_specs(config, [_UDP_59000, _TCP_8080], {8080: 40000})
    assert specs == expected_specs


def test_choose_host_ports_for_shared_guest_ports_pins_only_dual_protocol_ports_on_a_local_daemon() -> None:
    chosen = choose_host_ports_for_shared_guest_ports(_config(""), [_UDP_59000, _TCP_59000, _TCP_8080])
    assert set(chosen) == {59000}
    assert 1 <= chosen[59000] <= 65535


def test_choose_host_ports_for_shared_guest_ports_leaves_a_remote_daemon_to_docker() -> None:
    assert choose_host_ports_for_shared_guest_ports(_config("ssh://user@myserver"), [_UDP_59000, _TCP_59000]) == {}


def test_read_published_endpoints_from_port_bindings_resolves_each_declaration() -> None:
    port_bindings = {
        "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32770"}],
        "59000/udp": [{"HostIp": "127.0.0.1", "HostPort": "32771"}],
        "59000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32771"}],
    }
    endpoints = read_published_endpoints_from_port_bindings(port_bindings, [_UDP_59000, _TCP_59000], "127.0.0.1")
    assert endpoints == [
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="127.0.0.1", host_port=32771),
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.TCP, host="127.0.0.1", host_port=32771),
    ]


def test_read_published_endpoints_from_port_bindings_raises_for_an_unpublished_declaration() -> None:
    with pytest.raises(MngrError, match="59000/udp"):
        read_published_endpoints_from_port_bindings({"22/tcp": [{"HostPort": "1"}]}, [_UDP_59000], "127.0.0.1")


def test_build_docker_run_command_publishes_each_declaration_beside_sshd(temp_mngr_ctx: MngrContext) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    cmd = provider._build_docker_run_command(
        image="debian:bookworm-slim",
        container_name="test",
        labels={},
        start_args=(),
        publish_specs=["127.0.0.1::59000/udp", "127.0.0.1:40000:8080/tcp"],
    )
    publish_values = [cmd[index + 1] for index, arg in enumerate(cmd) if arg == "-p"]
    assert publish_values == ["127.0.0.1::22", "127.0.0.1::59000/udp", "127.0.0.1:40000:8080/tcp"]


def test_create_host_refuses_a_declaration_of_the_sshd_port_before_contacting_docker(
    temp_mngr_ctx: MngrContext,
) -> None:
    provider = make_docker_provider(temp_mngr_ctx)
    with pytest.raises(PublishedPortUnsupportedError, match="22/tcp"):
        provider.create_host(
            HostName("test-sshd-port"),
            published_ports=[PublishedPortDeclaration(guest_port=22, protocol=PortProtocol.TCP)],
        )
