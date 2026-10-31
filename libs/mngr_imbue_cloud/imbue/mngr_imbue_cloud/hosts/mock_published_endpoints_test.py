"""Shared in-memory published-endpoint source for host unit tests (imported explicitly, defines no tests)."""

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import PublishedEndpoint
from imbue.mngr_imbue_cloud.interfaces import LeasePublishedEndpointsInterface


class MockLeasePublishedEndpoints(MutableModel, LeasePublishedEndpointsInterface):
    """Answers a canned endpoint list for every host, recording which hosts asked."""

    endpoints: list[PublishedEndpoint] = []
    asked_host_ids: list[HostId] = []

    def published_endpoints_for(self, host_id: HostId) -> list[PublishedEndpoint]:
        self.asked_host_ids.append(host_id)
        return list(self.endpoints)
