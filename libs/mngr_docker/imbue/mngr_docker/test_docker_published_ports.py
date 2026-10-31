import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr.primitives import SnapshotName
from imbue.mngr_docker.instance import DockerProviderInstance
from imbue.mngr_docker.instance import create_docker_client

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

_DECLARATIONS = (
    PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.UDP),
    PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.TCP),
)


def _docker_bound_host_port(host: Host, declaration: PublishedPortDeclaration) -> int:
    """The host port Docker itself reports for the declaration, as `docker port` would print it."""
    containers = create_docker_client().containers.list(filters={"label": f"com.imbue.mngr.host-id={host.id}"})
    assert len(containers) == 1
    return int(containers[0].ports[str(declaration)][0]["HostPort"])


def _endpoint_by_protocol(host: Host) -> dict[PortProtocol, PublishedEndpoint]:
    return {endpoint.protocol: endpoint for endpoint in host.get_published_endpoints()}


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_create_host_publishes_declared_ports_on_loopback_at_the_ports_docker_bound(
    docker_provider: DockerProviderInstance,
) -> None:
    host = docker_provider.create_host(HostName("test-published-ports"), published_ports=_DECLARATIONS)

    endpoints = _endpoint_by_protocol(host)
    assert set(endpoints) == {PortProtocol.UDP, PortProtocol.TCP}
    for declaration in _DECLARATIONS:
        endpoint = endpoints[declaration.protocol]
        assert endpoint.guest_port == 59000
        assert endpoint.host == "127.0.0.1"
        assert endpoint.host_port == _docker_bound_host_port(host, declaration)
    # One guest port declared for both protocols is reachable at one host port number.
    assert endpoints[PortProtocol.UDP].host_port == endpoints[PortProtocol.TCP].host_port
    # The record the offline view reads carries the same endpoints.
    assert docker_provider.to_offline_host(host.id).get_published_endpoints() == host.get_published_endpoints()


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_published_endpoints_survive_stop_and_start(docker_provider: DockerProviderInstance) -> None:
    host = docker_provider.create_host(HostName("test-published-restart"), published_ports=_DECLARATIONS)
    endpoints_before = host.get_published_endpoints()
    docker_provider.stop_host(host, create_snapshot=False)

    assert docker_provider.get_host(host.id).get_published_endpoints() == endpoints_before

    restarted = docker_provider.start_host(host.id)
    assert isinstance(restarted, Host)
    assert restarted.get_published_endpoints() == endpoints_before
    for declaration in _DECLARATIONS:
        assert _endpoint_by_protocol(restarted)[declaration.protocol].host_port == _docker_bound_host_port(
            restarted, declaration
        )


@pytest.mark.docker
@pytest.mark.docker_sdk
def test_snapshot_restore_republishes_declared_ports_at_the_restored_containers_ports(
    docker_provider: DockerProviderInstance,
) -> None:
    host = docker_provider.create_host(HostName("test-published-snapshot"), published_ports=_DECLARATIONS)
    snapshot_id = docker_provider.create_snapshot(host.id, SnapshotName("published"))
    docker_provider.stop_host(host, create_snapshot=False)

    restored = docker_provider.start_host(host.id, snapshot_id=snapshot_id)

    assert isinstance(restored, Host)
    endpoints = _endpoint_by_protocol(restored)
    assert set(endpoints) == {PortProtocol.UDP, PortProtocol.TCP}
    for declaration in _DECLARATIONS:
        assert endpoints[declaration.protocol].host_port == _docker_bound_host_port(restored, declaration)
    assert endpoints[PortProtocol.UDP].host_port == endpoints[PortProtocol.TCP].host_port
