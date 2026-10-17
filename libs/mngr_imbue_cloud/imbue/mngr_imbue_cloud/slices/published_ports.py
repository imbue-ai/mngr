"""Published ports of a slice (specs/published-ports): the bridges between mngr's declarations, the slice scripts and the connector wire.

The slice scripts (``gen2_scripts``) ship into the connector container, where
``imbue.mngr`` does not exist, so they carry their own port models; this
module, which stays plugin-side, converts between those, the create's
``PublishedPortDeclaration``s, the connector's wire entries and the
``PublishedEndpoint``s the host record reports.
"""

from collections.abc import Sequence
from typing import assert_never

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import PortProtocol
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr.primitives import PublishedPortDeclaration
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortDeclaration
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortProtocol
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePublishedPort
from imbue.mngr_imbue_cloud.wire_types import PublishedPortInfo
from imbue.mngr_imbue_cloud.wire_types import PublishedPortProtocol


@pure
def _slice_protocol(protocol: PortProtocol) -> SlicePortProtocol:
    match protocol:
        case PortProtocol.TCP:
            return SlicePortProtocol.TCP
        case PortProtocol.UDP:
            return SlicePortProtocol.UDP
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _mngr_protocol(protocol: SlicePortProtocol) -> PortProtocol:
    match protocol:
        case SlicePortProtocol.TCP:
            return PortProtocol.TCP
        case SlicePortProtocol.UDP:
            return PortProtocol.UDP
        case _ as unreachable:
            assert_never(unreachable)


@pure
def to_slice_declarations(declarations: Sequence[PublishedPortDeclaration]) -> tuple[SlicePortDeclaration, ...]:
    """The create's declarations in the slice scripts' spelling."""
    return tuple(
        SlicePortDeclaration(guest_port=declaration.guest_port, protocol=_slice_protocol(declaration.protocol))
        for declaration in declarations
    )


@pure
def published_endpoints_from_slice_ports(
    entries: Sequence[SlicePublishedPort], box_address: str
) -> list[PublishedEndpoint]:
    """The host-record endpoints of a slice's published ports: every entry is reachable at the box's address."""
    return [
        PublishedEndpoint(
            guest_port=entry.guest_port,
            protocol=_mngr_protocol(entry.protocol),
            host=box_address,
            host_port=entry.host_port,
        )
        for entry in entries
    ]


def slice_declarations_from_wire(entries: Sequence[PublishedPortInfo]) -> tuple[SlicePortDeclaration, ...]:
    """The declarations behind a lease's wire entries, dropping any whose protocol this client does not know."""
    declarations: list[SlicePortDeclaration] = []
    for entry in entries:
        match entry.protocol:
            case PublishedPortProtocol.TCP:
                protocol = SlicePortProtocol.TCP
            case PublishedPortProtocol.UDP:
                protocol = SlicePortProtocol.UDP
            case PublishedPortProtocol.UNKNOWN:
                # The wire layer already warned once when it coerced the unknown value; this runs
                # on every listing, so it only traces.
                logger.trace(
                    "Ignoring published port {} with a protocol this client does not recognize", entry.guest_port
                )
                continue
            case _ as unreachable:
                assert_never(unreachable)
        declarations.append(SlicePortDeclaration(guest_port=entry.guest_port, protocol=protocol))
    return tuple(declarations)


@pure
def declarations_missing_from_wire(
    declarations: Sequence[PublishedPortDeclaration], entries: Sequence[PublishedPortInfo]
) -> list[PublishedPortDeclaration]:
    """The declared ports a lease's wire entries do not carry, in declaration order.

    Empty when the connector honoured every declaration; a connector from before published
    ports ignores the request's declarations and leases any host, which this exposes.
    """
    published = {(entry.guest_port, entry.protocol.value) for entry in entries}
    return [
        declaration
        for declaration in declarations
        if (declaration.guest_port, declaration.protocol.value.lower()) not in published
    ]


def published_endpoints_from_wire(entries: Sequence[PublishedPortInfo], box_address: str) -> list[PublishedEndpoint]:
    """The host-record endpoints of a lease's wire entries, reachable at the box's address.

    An entry whose protocol this client does not recognize is left out
    (``slice_declarations_from_wire`` drops it) rather than reported under a
    guessed protocol.
    """
    declarations = slice_declarations_from_wire(entries)
    known_entries = [entry for entry in entries if entry.protocol is not PublishedPortProtocol.UNKNOWN]
    return published_endpoints_from_slice_ports(
        tuple(
            SlicePublishedPort(
                guest_port=declaration.guest_port, protocol=declaration.protocol, host_port=entry.host_port
            )
            for declaration, entry in zip(declarations, known_entries, strict=True)
        ),
        box_address,
    )


@pure
def build_slice_container_publish_start_args(declarations: Sequence[SlicePortDeclaration]) -> tuple[str, ...]:
    """The ``docker run`` flags that publish each declared guest port inside the slice VM on the same number.

    ``-p 0.0.0.0:<guest_port>:<guest_port>/<protocol>`` per declaration, so
    the box's DNAT target (``<vm_ip>:<guest_port>``) needs no second mapping.
    """
    args: list[str] = []
    for declaration in declarations:
        args.extend(["-p", f"0.0.0.0:{declaration.guest_port}:{declaration.guest_port}/{declaration.protocol.value}"])
    return tuple(args)
