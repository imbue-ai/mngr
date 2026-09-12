"""Helpers shared by the imbue_cloud provider unit tests."""

from pathlib import Path

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import load_host_key_record


def load_pins_by_endpoint(known_hosts_path: Path, host_id: HostId) -> dict[tuple[str, int], tuple[str, HostKeyOrigin]]:
    """The host's pins as ``{(address, port): (public_key, origin)}``; the host must have a record."""
    record = load_host_key_record(known_hosts_path, host_id)
    assert record is not None
    return {(pin.address, pin.port): (pin.public_key, pin.origin) for pin in record.pins}
