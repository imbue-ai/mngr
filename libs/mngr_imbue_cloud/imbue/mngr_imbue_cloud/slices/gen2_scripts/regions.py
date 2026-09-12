from collections.abc import Mapping
from typing import Final

# The OVH-US regions the imbue_cloud host pool can land hosts in (the lease-region
# labels stamped on pool rows), each mapped to the OVH datacenter code serving it,
# as used by the OVH order/catalog and ``/dedicated/server/datacenter/availabilities``
# APIs and stored in ``bare_metal_servers.region``: ``vin`` = Vint Hill,
# ``hil`` = Hillsboro. The single source for the pairing: the plugin's
# ``primitives`` derives its region/datacenter collections from it, and the
# connector maps a pool row's lease-region label to the boxes that can host
# its restore with it. It lives in this leaf subpackage because that is the
# only part of the plugin the connector container mounts. Kept small and
# explicit on purpose; extend when the pool gains new datacenters.
OVH_DATACENTER_CODE_BY_US_REGION: Final[Mapping[str, str]] = {"US-EAST-VA": "vin", "US-WEST-OR": "hil"}
