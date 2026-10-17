import pytest

from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr_vps.published_ports import reject_declarations_outside_firewall
from imbue.mngr_vps.published_ports import reject_vm_port_collisions
from imbue.mngr_vps.published_ports import vps_publish_specs
from imbue.mngr_vps.published_ports import vps_published_endpoints

_UDP_59000 = PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.UDP)
_TCP_8080 = PublishedPortDeclaration(guest_port=8080, protocol=PortProtocol.TCP)


def test_vps_publish_specs_publish_each_guest_port_at_its_own_number_on_every_interface() -> None:
    assert vps_publish_specs([_UDP_59000, _TCP_8080]) == ["0.0.0.0:59000:59000/udp", "0.0.0.0:8080:8080/tcp"]


def test_vps_published_endpoints_are_the_vm_address_at_the_guest_port() -> None:
    assert vps_published_endpoints("203.0.113.9", [_UDP_59000]) == [
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="203.0.113.9", host_port=59000)
    ]


def test_reject_declarations_outside_firewall_passes_listed_ports_and_refuses_others() -> None:
    provider_name = ProviderInstanceName("aws")
    reject_declarations_outside_firewall(provider_name, [_UDP_59000], [_UDP_59000, _TCP_8080], "mngr aws prepare")
    with pytest.raises(PublishedPortUnsupportedError, match="8080/tcp.*mngr aws prepare"):
        reject_declarations_outside_firewall(provider_name, [_UDP_59000, _TCP_8080], [_UDP_59000], "mngr aws prepare")


def test_reject_vm_port_collisions_refuses_the_vm_sshd_and_the_container_sshd_ports() -> None:
    provider_name = ProviderInstanceName("ovh")
    reject_vm_port_collisions(provider_name, [_UDP_59000, _TCP_8080], container_ssh_port=2222)
    with pytest.raises(PublishedPortUnsupportedError, match="22/tcp.*sshd"):
        reject_vm_port_collisions(
            provider_name,
            [PublishedPortDeclaration(guest_port=22, protocol=PortProtocol.TCP)],
            container_ssh_port=2222,
        )
    with pytest.raises(PublishedPortUnsupportedError, match="2222/udp.*container_ssh_port"):
        reject_vm_port_collisions(
            provider_name,
            [PublishedPortDeclaration(guest_port=2222, protocol=PortProtocol.UDP)],
            container_ssh_port=2222,
        )
