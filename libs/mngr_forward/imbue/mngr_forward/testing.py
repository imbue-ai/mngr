"""Test helpers for mngr_forward unit + integration tests.

Per CLAUDE.md, do not create tests for testing.py itself; the helpers are
exercised through the tests that import them.
"""

from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import HostId
from imbue.mngr_forward.tls import LocalCertificateAuthority
from imbue.mngr_forward.tls import _build_ca_certificate
from imbue.mngr_forward.tls import _generate_rsa_key
from imbue.mngr_forward.tls import _key_to_pem


def make_in_memory_test_ca() -> LocalCertificateAuthority:
    """Build a throwaway in-memory CA (no filesystem persistence) for tests."""
    ca_key = _generate_rsa_key()
    return LocalCertificateAuthority(cert_pem=_build_ca_certificate(ca_key), key_pem=_key_to_pem(ca_key))


# A trio of canned, well-formed agent IDs for use in tests. AgentId is a
# RandomId requiring exactly 32 hex chars after the ``agent-`` prefix; we
# use deterministic constants so test output is stable.
TEST_AGENT_ID_1: AgentId = AgentId("agent-" + "0" * 31 + "1")
TEST_AGENT_ID_2: AgentId = AgentId("agent-" + "0" * 31 + "2")
TEST_AGENT_ID_3: AgentId = AgentId("agent-" + "0" * 31 + "3")

# Canned host ids and the matching agent instance keys (the resolver is
# instance-keyed: agent ids are unique per host, not globally).
TEST_HOST_ID_1: HostId = HostId("host-" + "0" * 31 + "a")
TEST_HOST_ID_2: HostId = HostId("host-" + "0" * 31 + "b")
TEST_INSTANCE_1: AgentInstanceKey = AgentInstanceKey.build(TEST_AGENT_ID_1, TEST_HOST_ID_1)
TEST_INSTANCE_2: AgentInstanceKey = AgentInstanceKey.build(TEST_AGENT_ID_2, TEST_HOST_ID_2)
TEST_INSTANCE_2_ON_HOST_1: AgentInstanceKey = AgentInstanceKey.build(TEST_AGENT_ID_2, TEST_HOST_ID_1)
# The same agent id as TEST_INSTANCE_1 living on another host -- the
# duplicate-id (migration overlap) case.
TEST_INSTANCE_1_ON_HOST_2: AgentInstanceKey = AgentInstanceKey.build(TEST_AGENT_ID_1, TEST_HOST_ID_2)
