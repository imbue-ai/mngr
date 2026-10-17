import re

import pytest

from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import InvalidSliceOrdinalError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import MalformedBoxOutputError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import MalformedPublishedPortsError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_HOST_VOLUME_MOUNT_PATH
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_HOST_QUOTA_QGROUP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HOST_ID_CONTAINER_LABEL
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_PUBLISHED_PORT_ENTRY_BASH_RE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_TC_MARK_BASE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import Gen2Reservation
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortDeclaration
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePortProtocol
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import SlicePublishedPort
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import assign_published_host_ports
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import derive_slice_network
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import distinct_guest_ports
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_disk_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_instance_dir
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import is_rendered_published_port_entry
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import parse_published_ports_env_value
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import parse_reservation_marker_line
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import published_port_placeholder
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import render_published_ports_env_template_value
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import render_published_ports_env_value
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_mac_address
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_tap_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unit_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unix_user
from imbue.mngr_vps.container_setup import HOST_QUOTA_QGROUP
from imbue.mngr_vps.container_setup import HOST_VOLUME_MOUNT_PATH
from imbue.mngr_vps.container_setup import LABEL_HOST_ID

_UDP_59000 = SlicePortDeclaration(guest_port=59000, protocol=SlicePortProtocol.UDP)
_TCP_59000 = SlicePortDeclaration(guest_port=59000, protocol=SlicePortProtocol.TCP)
_TCP_8080 = SlicePortDeclaration(guest_port=8080, protocol=SlicePortProtocol.TCP)


def test_derive_slice_network_gives_each_ordinal_its_own_30() -> None:
    first = derive_slice_network(0)
    assert (first.gateway_ip, first.vm_ip, first.prefix_length) == ("10.201.0.1", "10.201.0.2", 30)
    third = derive_slice_network(3)
    assert (third.gateway_ip, third.vm_ip) == ("10.201.0.13", "10.201.0.14")
    # 512 ordinals x 4 addresses fit comfortably inside the reserved
    # 10.201.0.0/16 (the top /30 sits in 10.201.7.0/24).
    last = derive_slice_network(GEN2_MAX_SLICE_COUNT - 1)
    assert last.vm_ip == "10.201.7.254"
    assert last.gateway_ip == "10.201.7.253"


def test_ordinal_derived_names_are_deterministic() -> None:
    assert slice_tap_name(7) == "mslice7"
    assert slice_unix_user(7) == "mngr-slice-7"
    assert slice_unit_name(7) == "mngr-slice@7"
    assert slice_mac_address(7) == "52:54:00:6d:00:07"


def test_ordinal_encodings_hold_across_the_full_512_range() -> None:
    # Every per-ordinal encoding stays valid at the 512 bound: the tap name
    # fits IFNAMSIZ (15), the MAC's low bytes carry the ordinal, each /30 is
    # disjoint from its neighbor, and the tc class minor / mark stay in range.
    seen_vm_ips: set[str] = set()
    for ordinal in range(GEN2_MAX_SLICE_COUNT):
        assert len(slice_tap_name(ordinal)) <= 15
        mac_parts = slice_mac_address(ordinal).split(":")
        assert (int(mac_parts[4], 16) << 8) + int(mac_parts[5], 16) == ordinal
        network = derive_slice_network(ordinal)
        assert network.vm_ip not in seen_vm_ips
        seen_vm_ips.add(network.vm_ip)
        assert 0 < GEN2_TC_MARK_BASE + ordinal <= 0xFFFFFFFF
        assert 16 + ordinal <= 0xFFFF


def test_ordinal_out_of_range_is_rejected() -> None:
    with pytest.raises(InvalidSliceOrdinalError):
        derive_slice_network(GEN2_MAX_SLICE_COUNT)
    with pytest.raises(InvalidSliceOrdinalError):
        slice_tap_name(-1)


def test_instance_dir_and_disk_name_follow_the_instance_name() -> None:
    assert gen2_instance_dir("mngr-slice-dev-x-abc") == "/srv/mngr-slices/instances/mngr-slice-dev-x-abc"
    assert gen2_disk_name("mngr-slice-dev-x-abc") == "mngr-slice-dev-x-abc-data"


def test_guest_contract_constants_match_the_mngr_vps_originals() -> None:
    # The subpackage ships into the connector container without mngr_vps, so it
    # carries copies of the contracts the in-guest scripts depend on.
    assert GEN2_HOST_ID_CONTAINER_LABEL == LABEL_HOST_ID
    assert GEN2_GUEST_HOST_QUOTA_QGROUP == HOST_QUOTA_QGROUP
    assert GEN2_CONTAINER_HOST_VOLUME_MOUNT_PATH == HOST_VOLUME_MOUNT_PATH


def test_distinct_guest_ports_shares_one_box_port_across_both_protocols() -> None:
    assert distinct_guest_ports((_UDP_59000, _TCP_59000, _TCP_8080)) == (59000, 8080)
    assert distinct_guest_ports(()) == ()


def test_assign_published_host_ports_pairs_each_declaration_with_its_guest_ports_box_port() -> None:
    assigned = assign_published_host_ports((_UDP_59000, _TCP_59000, _TCP_8080), (22012, 22013))
    assert assigned == (
        SlicePublishedPort(guest_port=59000, protocol=SlicePortProtocol.UDP, host_port=22012),
        SlicePublishedPort(guest_port=59000, protocol=SlicePortProtocol.TCP, host_port=22012),
        SlicePublishedPort(guest_port=8080, protocol=SlicePortProtocol.TCP, host_port=22013),
    )
    assert assigned[0].declaration == _UDP_59000


def test_assign_published_host_ports_refuses_a_count_mismatch() -> None:
    with pytest.raises(MalformedBoxOutputError):
        assign_published_host_ports((_UDP_59000, _TCP_8080), (22012,))
    with pytest.raises(MalformedBoxOutputError):
        assign_published_host_ports((), (22012,))


def test_published_ports_env_value_round_trips_and_reads_an_absent_value_as_none() -> None:
    entries = assign_published_host_ports((_UDP_59000, _TCP_59000, _TCP_8080), (22012, 22013))
    rendered = render_published_ports_env_value(entries)
    # Compact JSON (no whitespace) so the helper's regex and iterator can read it.
    assert " " not in rendered
    assert parse_published_ports_env_value(rendered) == entries
    assert parse_published_ports_env_value("") == ()
    assert parse_published_ports_env_value("[]") == ()


def test_published_ports_env_value_rejects_malformed_json() -> None:
    for malformed in ("not json", '{"guest_port":1}', "[1]", '[{"guest_port":1,"protocol":"icmp","host_port":2}]'):
        with pytest.raises(MalformedPublishedPortsError):
            parse_published_ports_env_value(malformed)


def test_rendered_entries_match_the_helpers_bash_regex() -> None:
    # The helper validates the env value with this ERE; every entry the
    # renderer writes must match it (and the Python twin), or the setup verb
    # would refuse the slice's own env file.
    entries = assign_published_host_ports((_UDP_59000, _TCP_8080), (22012, 22013))
    rendered = render_published_ports_env_value(entries)
    entry_texts = [match.group(0) for match in re.finditer(GEN2_PUBLISHED_PORT_ENTRY_BASH_RE, rendered)]
    assert entry_texts == [
        '{"guest_port":59000,"protocol":"udp","host_port":22012}',
        '{"guest_port":8080,"protocol":"tcp","host_port":22013}',
    ]
    assert all(is_rendered_published_port_entry(entry_text) for entry_text in entry_texts)
    assert not is_rendered_published_port_entry('{"guest_port":1,"protocol":"tcp"}')


def test_env_template_value_carries_one_placeholder_per_distinct_guest_port() -> None:
    template = render_published_ports_env_template_value((_UDP_59000, _TCP_59000, _TCP_8080))
    assert template.count(published_port_placeholder(0)) == 2
    assert template.count(published_port_placeholder(1)) == 1
    assert published_port_placeholder(2) not in template
    assert render_published_ports_env_template_value(()) == "[]"


def test_parse_reservation_marker_line_reads_the_published_ports_after_the_ordinal() -> None:
    assert parse_reservation_marker_line("noise\nMARK 22010 22011 3 22012 22013\n", "MARK") == Gen2Reservation(
        vm_ssh_host_port=22010, container_ssh_host_port=22011, ordinal=3, published_host_ports=(22012, 22013)
    )
    # The three-field line an older reserve prints still parses.
    assert parse_reservation_marker_line("MARK 22010 22011 3", "MARK") == Gen2Reservation(
        vm_ssh_host_port=22010, container_ssh_host_port=22011, ordinal=3
    )
    assert parse_reservation_marker_line("MARK 22010 22011", "MARK") is None
    assert parse_reservation_marker_line("MARK 22010 oops 3", "MARK") is None
    assert parse_reservation_marker_line("MARKER 22010 22011 3", "MARK") is None
    assert parse_reservation_marker_line("nothing", "MARK") is None
