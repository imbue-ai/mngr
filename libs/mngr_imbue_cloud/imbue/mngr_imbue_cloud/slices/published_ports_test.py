from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import parse_published_port_declarations
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortDeclaration
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortProtocol
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePublishedPort
from imbue.mngr_imbue_cloud.slices.published_ports import build_slice_container_publish_start_args
from imbue.mngr_imbue_cloud.slices.published_ports import declarations_missing_from_wire
from imbue.mngr_imbue_cloud.slices.published_ports import published_endpoints_from_slice_ports
from imbue.mngr_imbue_cloud.slices.published_ports import published_endpoints_from_wire
from imbue.mngr_imbue_cloud.slices.published_ports import slice_declarations_from_wire
from imbue.mngr_imbue_cloud.slices.published_ports import to_slice_declarations
from imbue.mngr_imbue_cloud.wire_types import PublishedPortInfo
from imbue.mngr_imbue_cloud.wire_types import PublishedPortProtocol


def test_declarations_convert_to_the_slice_spelling_and_to_publish_flags() -> None:
    slice_declarations = to_slice_declarations(parse_published_port_declarations(["59000/udp", "59000", "8080/tcp"]))
    assert slice_declarations == (
        SlicePortDeclaration(guest_port=59000, protocol=SlicePortProtocol.UDP),
        SlicePortDeclaration(guest_port=59000, protocol=SlicePortProtocol.TCP),
        SlicePortDeclaration(guest_port=8080, protocol=SlicePortProtocol.TCP),
    )
    # The container publishes each guest port inside the VM on the same number
    # and every interface, which is where the box's DNAT for it lands.
    assert build_slice_container_publish_start_args(slice_declarations) == (
        "-p",
        "0.0.0.0:59000:59000/udp",
        "-p",
        "0.0.0.0:59000:59000/tcp",
        "-p",
        "0.0.0.0:8080:8080/tcp",
    )
    assert build_slice_container_publish_start_args(()) == ()


def test_slice_ports_become_endpoints_at_the_box_address() -> None:
    entries = (
        SlicePublishedPort(guest_port=59000, protocol=SlicePortProtocol.UDP, host_port=22012),
        SlicePublishedPort(guest_port=59000, protocol=SlicePortProtocol.TCP, host_port=22012),
    )
    assert published_endpoints_from_slice_ports(entries, "203.0.113.9") == [
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="203.0.113.9", host_port=22012),
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.TCP, host="203.0.113.9", host_port=22012),
    ]


def test_wire_entries_with_an_unknown_protocol_are_dropped_not_guessed() -> None:
    entries = (
        PublishedPortInfo(guest_port=59000, protocol=PublishedPortProtocol.UDP, host_port=22012),
        PublishedPortInfo(guest_port=7000, protocol=PublishedPortProtocol.UNKNOWN, host_port=22013),
        PublishedPortInfo(guest_port=8080, protocol=PublishedPortProtocol.TCP, host_port=22014),
    )
    assert slice_declarations_from_wire(entries) == (
        SlicePortDeclaration(guest_port=59000, protocol=SlicePortProtocol.UDP),
        SlicePortDeclaration(guest_port=8080, protocol=SlicePortProtocol.TCP),
    )
    assert published_endpoints_from_wire(entries, "203.0.113.9") == [
        PublishedEndpoint(guest_port=59000, protocol=PortProtocol.UDP, host="203.0.113.9", host_port=22012),
        PublishedEndpoint(guest_port=8080, protocol=PortProtocol.TCP, host="203.0.113.9", host_port=22014),
    ]
    assert published_endpoints_from_wire((), "203.0.113.9") == []


def test_declarations_missing_from_wire_names_the_declared_ports_the_lease_lacks() -> None:
    declarations = parse_published_port_declarations(["59000/udp", "59000/tcp", "8080"])
    entries = (
        PublishedPortInfo(guest_port=59000, protocol=PublishedPortProtocol.UDP, host_port=22012),
        PublishedPortInfo(guest_port=8080, protocol=PublishedPortProtocol.TCP, host_port=22014),
    )
    assert declarations_missing_from_wire(declarations, entries) == [declarations[1]]
    assert declarations_missing_from_wire(declarations, ()) == list(declarations)
    # A lease that publishes more than was declared still honours every declaration.
    assert declarations_missing_from_wire(declarations[:1], entries) == []
    assert declarations_missing_from_wire((), ()) == []
