"""Non-fixture test utilities shared across mngr_wait tests.

``create_agent_data_json`` is re-exported from core so the wait tests and the
core single-target tests share one implementation.
"""

from imbue.mngr.api.testing import create_agent_data_json as create_agent_data_json
