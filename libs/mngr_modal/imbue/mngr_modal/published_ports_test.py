from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import PublishedPortUnsupportedError
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr_modal.instance import ModalProviderInstance
from imbue.mngr_modal.instance import SandboxConfig
from imbue.mngr_modal.published_ports import published_endpoints_from_tunnels
from imbue.mngr_modal.published_ports import sandbox_unencrypted_ports
from imbue.mngr_modal.testing import make_testing_provider
from imbue.modal_proxy.data_types import TunnelInfo
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ImageInterface
from imbue.modal_proxy.interface import SandboxInterface
from imbue.modal_proxy.interface import VolumeInterface
from imbue.modal_proxy.testing import FakeModalInterface

_TCP_59000 = PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.TCP)
_UDP_59000 = PublishedPortDeclaration(guest_port=59000, protocol=PortProtocol.UDP)


class _PortRecordingFakeModalInterface(FakeModalInterface):
    """A FakeModalInterface that records the ``unencrypted_ports`` each sandbox was created with."""

    _unencrypted_ports_by_create: list[tuple[int, ...]] = PrivateAttr(default_factory=list)

    def sandbox_create(
        self,
        *,
        image: ImageInterface,
        app: AppInterface,
        timeout: int,
        cpu: float,
        memory: int,
        unencrypted_ports: Sequence[int] = (),
        gpu: str | None = None,
        region: str | None = None,
        cidr_allowlist: Sequence[str] | None = None,
        volumes: Mapping[str, VolumeInterface] | None = None,
        experimental_options: Mapping[str, bool] | None = None,
    ) -> SandboxInterface:
        self._unencrypted_ports_by_create.append(tuple(unencrypted_ports))
        return super().sandbox_create(
            image=image,
            app=app,
            timeout=timeout,
            cpu=cpu,
            memory=memory,
            unencrypted_ports=unencrypted_ports,
            gpu=gpu,
            region=region,
            cidr_allowlist=cidr_allowlist,
            volumes=volumes,
            experimental_options=experimental_options,
        )

    def recorded_unencrypted_ports(self) -> list[tuple[int, ...]]:
        return list(self._unencrypted_ports_by_create)


def test_create_running_sandbox_requests_a_tunnel_for_each_published_tcp_port(
    temp_mngr_ctx: MngrContext, tmp_path: Path, cg: ConcurrencyGroup
) -> None:
    root = tmp_path / f"modal_testing_{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    modal_interface = _PortRecordingFakeModalInterface(root_dir=root, concurrency_group=cg)
    try:
        provider = make_testing_provider(temp_mngr_ctx, modal_interface)
        provider._create_running_sandbox(
            image=provider._modal_interface.image_debian_slim(),
            app=provider._get_modal_app(),
            config=SandboxConfig(published_ports=(_TCP_59000,)),
            volumes={},
        )
    finally:
        modal_interface.cleanup()

    assert modal_interface.recorded_unencrypted_ports() == [(22, 59000)]


def test_create_host_refuses_a_udp_declaration_before_any_sandbox_is_created(
    testing_provider: ModalProviderInstance, testing_modal: FakeModalInterface
) -> None:
    with pytest.raises(PublishedPortUnsupportedError, match="59000/udp.*TCP only"):
        testing_provider.create_host(HostName("udp-host"), published_ports=[_UDP_59000])

    assert testing_modal.get_sandbox_count() == 0


def test_published_endpoints_from_tunnels_reads_each_declared_ports_tunnel() -> None:
    tunnels = {
        22: TunnelInfo(tcp_socket=("r1.modal.host", 22222)),
        59000: TunnelInfo(tcp_socket=("r1.modal.host", 41000)),
    }
    assert published_endpoints_from_tunnels(tunnels, [_TCP_59000]) == [
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.TCP, host="r1.modal.host", host_port=41000)
    ]
    with pytest.raises(MngrError, match="8080/tcp"):
        published_endpoints_from_tunnels(
            tunnels, [PublishedPortDeclaration(guest_port=8080, protocol=PortProtocol.TCP)]
        )


def test_sandbox_unencrypted_ports_lists_sshd_first_and_each_guest_port_once() -> None:
    assert sandbox_unencrypted_ports(22, [_TCP_59000, _TCP_59000]) == [22, 59000]
    assert sandbox_unencrypted_ports(22, []) == [22]
