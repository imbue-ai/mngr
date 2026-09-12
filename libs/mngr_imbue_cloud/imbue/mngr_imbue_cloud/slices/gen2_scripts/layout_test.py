import pytest

from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import InvalidSliceOrdinalError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GUEST_HOST_QUOTA_QGROUP
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_HOST_ID_CONTAINER_LABEL
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAX_SLICE_COUNT
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_TC_MARK_BASE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import derive_slice_network
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_disk_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_instance_dir
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_mac_address
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_tap_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unit_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unix_user
from imbue.mngr_vps.container_setup import HOST_QUOTA_QGROUP
from imbue.mngr_vps.container_setup import LABEL_HOST_ID


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
    # carries copies of the two contracts the in-guest scripts depend on.
    assert GEN2_HOST_ID_CONTAINER_LABEL == LABEL_HOST_ID
    assert GEN2_GUEST_HOST_QUOTA_QGROUP == HOST_QUOTA_QGROUP
